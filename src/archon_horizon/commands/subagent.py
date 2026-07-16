"""Invoke descriptor-backed subagents from the CLI or agent wrapper."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.config.harnesses import HarnessRegistry
from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.log import log
from archon_horizon.runlog import RunLogTree, SessionLog
from archon_horizon.subagents.base import SubagentContext
from archon_horizon.subagents.registry import build_registry

from .shared import emit_json


class SubagentCommand:
    def __init__(
        self,
        root: Path,
        *,
        name: str,
        slug: str,
        directive_file: Path,
        parent_log_dir: Path | None = None,
        write_domain: tuple[str, ...] = (),
        as_json: bool = False,
    ) -> None:
        self.root = root
        self.name = name
        self.slug = slug
        self.directive_file = directive_file
        self.parent_log_dir = parent_log_dir
        self.write_domain = write_domain
        self.as_json = as_json

    def _session(self, workspace) -> SessionLog:
        if self.parent_log_dir is not None:
            return SessionLog(self.parent_log_dir).new_subsession(self.name)
        run = RunLogTree(workspace.state_path / "runs").allocate()
        session = run.new_session(f"subagent-{self.name}")
        session.write_meta({"role": "subagent-run", "name": self.name})
        return session

    def run(self) -> None:
        if not self.directive_file.exists():
            log.error(f"Directive file not found: {self.directive_file}")
            raise typer.Exit(1)

        cfg = load_config(self.root)
        workspace = build_workspace(cfg, self.root)
        harnesses = HarnessRegistry().build_all(cfg.harnesses)
        default_harness = harnesses.get(cfg.horizon_harness or "")
        registry = build_registry(
            workspace.state_path / "subagents",
            harnesses=harnesses,
            default_harness=default_harness,
        )
        subagent = registry.get(self.name)
        if subagent is None:
            log.error(f"Unknown subagent {self.name!r}. Available: {', '.join(registry.names()) or '<none>'}")
            raise typer.Exit(2)

        session = self._session(workspace)
        directive = self.directive_file.read_text("utf-8")
        result = subagent.run(SubagentContext(
            workspace=workspace,
            log_dir=session.path,
            directive=directive,
            slug=self.slug,
            write_domain=self.write_domain,
        ))
        session.write_meta({
            "role": "subagent",
            "name": self.name,
            "slug": self.slug,
            "ok": result.ok,
            "write_domain": list(self.write_domain),
            "data": result.data,
        })
        # Persist the subagent's report. Prefer what the subagent returned; if
        # that's empty (e.g. it was blocked from writing and we lost the text),
        # recover it from the streamed transcript so a report.md always exists.
        report_text = result.report
        if not report_text and session.transcript_path.exists():
            from archon_horizon.transcript.parsers import aggregate
            from archon_horizon.transcript.sink import read_transcript

            report_text, _ = aggregate(read_transcript(session.transcript_path))
        if report_text:
            report_path = session.path / "report.md"
            report_path.write_text(report_text, "utf-8")
        if self.as_json:
            emit_json({"name": self.name, "slug": self.slug, "ok": result.ok, "session": str(session.path)})
        else:
            log.success(f"{self.name}/{self.slug} complete: {session.path}")
        raise typer.Exit(0 if result.ok else 1)


def subagent(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Subagent name."),
    slug: str = typer.Option(..., "--slug", help="Stable slug for this dispatch."),
    directive_file: Path = typer.Option(..., "--directive-file", help="Markdown directive file."),
    parent_log_dir: Path | None = typer.Option(
        None,
        "--parent-log-dir",
        help="Parent session directory; child logs are nested below it.",
    ),
    write_domain: list[str] = typer.Option(
        None,
        "--write-domain",
        help="Glob/path this subagent may write. Repeat for multiple domains.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Run a subagent through its configured harness."""
    SubagentCommand(
        ctx.obj["root"],
        name=name,
        slug=slug,
        directive_file=directive_file,
        parent_log_dir=parent_log_dir,
        write_domain=tuple(write_domain or ()),
        as_json=as_json,
    ).run()
