"""Typer-decorated ``run`` entry point."""

from __future__ import annotations

import hashlib
import dataclasses
from pathlib import Path

import typer

from archon_horizon.config.harnesses import HarnessRegistry
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.log import log

from .shared import emit_json, inbox_providers, load_workspace

# The single-agent run targets. ``horizon run ground`` / ``horizon run horizon``
# drive exactly one session of that role instead of the usual G/H alternation.
ROLE_TARGETS = ("ground", "horizon")


class RunCommand:
    def __init__(
        self,
        root: Path,
        *,
        targets: tuple[str, ...] = (),
        task: str | None = None,
        rounds: int | None = None,
        dry_run: bool = False,
        resume: str | None = None,
        backend: str = "default",
        as_json: bool = False,
    ) -> None:
        self.root = root
        self.targets = targets
        self.task = task
        self.rounds = rounds
        self.dry_run = dry_run
        self.resume = resume
        self.backend = (backend or "default").strip().lower()
        self.as_json = as_json

    def run(self) -> None:
        if not self.targets and self.task:
            self.targets = (self.task,)

        # `--backend interactive` hands the terminal straight to the engine so a
        # human can drive the agent (type follow-ups). It only makes sense for a
        # single role, not the headless orchestrated alternation.
        if self.backend == "interactive":
            self._run_interactive()
            return

        cfg, workspace = load_workspace(self.root)
        self._install_native_subagents(cfg, workspace)
        _, providers = inbox_providers(cfg, workspace)
        orch = build_orchestrator(self.root, registry=HarnessRegistry(), inbox_providers=providers)

        if self.resume is not None:
            run = self._resume_run(orch)
            reports = orch.run(run, resume=True)
            self._emit_reports(reports)
            return

        if not self.targets:
            log.error("Specify what to run: `horizon run .`, `horizon run '*'`, `ground`, `horizon`, task names, project names, or files.")
            raise typer.Exit(1)

        # `horizon run ground` / `horizon run horizon`: one session of that role.
        if len(self.targets) == 1 and self.targets[0] in ROLE_TARGETS:
            reports = self._run_single_role(orch, self.targets[0])
            self._emit_reports(reports)
            return

        focus = self._resolve_focus(orch, tuple(self.targets))
        run = RunRecord(id="", focus=focus, rounds_requested=self.rounds or cfg.rounds)
        reports = orch.run(run, dry_run=self.dry_run)
        self._emit_reports(reports)

    def _run_single_role(self, orch, role: str):
        """Drive exactly one session of ``role``.

        Ground: run the opening plan only (``rounds=0`` runs the opener, then the
        loop body never executes). Horizon: skip the opening and closing Ground
        (``start_with``/``end_with`` = ``horizon``) and run a single round, so the
        run is one bare Horizon step over the current focus."""
        if role == "ground":
            orch.start_with, orch.end_with = "ground", "ground"
            run = RunRecord(id="", focus=Focus(), rounds_requested=0)
        else:  # horizon
            orch.start_with, orch.end_with = "horizon", "horizon"
            focus = Focus() if not self.targets[1:] else self._resolve_focus(orch, self.targets[1:])
            run = RunRecord(id="", focus=focus, rounds_requested=1)
        return orch.run(run, dry_run=self.dry_run)

    def _run_interactive(self) -> None:
        """Launch the role's harness as an interactive TTY session, seeded with a
        role prompt the human can then steer."""
        from .interactive import interactive_launch_for_role, interactive_role_prompt, run_interactive

        role = self.targets[0] if self.targets and self.targets[0] in ROLE_TARGETS else "ground"
        if not self.targets or self.targets[0] not in ROLE_TARGETS:
            log.info(f"`--backend interactive` runs a single role; defaulting to {role!r}. "
                     "Pass `horizon run ground` or `horizon run horizon` to choose.")
        prompt = interactive_role_prompt(self.root.resolve(), role)
        try:
            launch = interactive_launch_for_role(self.root, role, prompt)
        except Exception as exc:
            log.error(f"Could not launch an interactive {role} session: {exc}")
            raise typer.Exit(1)
        if launch is None:
            log.error(f"The {role} harness is 'null'; nothing to launch interactively.")
            raise typer.Exit(1)
        log.info(f"Launching an interactive {role} session using {launch.description}.")
        run_interactive(launch, self.root)

    def _install_native_subagents(self, cfg, workspace) -> None:
        """Compile descriptors into each engine's workspace-local native agents.

        Done at run start so a change to the descriptors or a harness's tier map
        takes effect on the next run, with no separate install step.
        """
        try:
            from archon_horizon.subagents.compile import install_subagents

            install_subagents(workspace.root, workspace.state_path / "subagents", cfg.harnesses)
        except Exception as exc:  # never block a run on optional subagent compile
            log.warn(f"Skipped subagent compilation: {exc}")

    def _resume_run(self, orch) -> RunRecord:
        """Reconstruct the run to resume: the named run id, else the latest one."""
        run_id = (self.resume or "").strip()
        if run_id.lower() in ("", "latest", "last"):
            ids = orch.run_logs.ids() if orch.run_logs is not None else []
            if not ids:
                log.error("Nothing to resume: this workspace has no recorded runs.")
                raise typer.Exit(1)
            run_id = ids[-1]
        try:
            run = orch.run_store.get(run_id)
        except Exception:
            log.error(f"Cannot resume run {run_id!r}: no run record found.")
            raise typer.Exit(1)
        log.step(f"Resuming run {run_id} from its interrupted round.")
        return run

    def _emit_reports(self, reports) -> None:
        rows: list[tuple[str, str, str]] = []
        summary: list[dict] = []
        for report in reports:
            if self.dry_run:
                summary.append({"round": report.round_index, "planned": list(report.planned)})
                rows.append((f"round {report.round_index}", "planned", ", ".join(report.planned) or "-"))
            else:
                summary.append({
                    "round": report.round_index,
                    "tasks_run": list(report.tasks_run),
                    "tasks_blocked": list(report.tasks_blocked),
                })
                detail = f"ran {', '.join(report.tasks_run) or '-'}; blocked {', '.join(report.tasks_blocked) or '-'}"
                rows.append((f"round {report.round_index}", "done", detail))
        if self.as_json:
            emit_json({"dry_run": self.dry_run, "rounds": summary})
            return
        log.results_table(rows, title="Run")

    def _resolve_focus(self, orch, targets: tuple[str, ...]) -> Focus:
        if not targets:
            return Focus()
        if "*" in targets:
            if len(targets) != 1:
                log.error("`*` must be the only run target.")
                raise typer.Exit(1)
            return Focus()

        known_tasks = {task.id: task for task in orch.task_store.list()}
        selected: list[str] = []
        remaining: list[str] = []
        for target in targets:
            if target in known_tasks:
                selected.append(target)
            else:
                remaining.append(target)

        if remaining:
            selected.append(self._ensure_ad_hoc_task(orch, tuple(remaining)).id)
        return Focus(tasks=tuple(dict.fromkeys(selected)))

    def _ensure_ad_hoc_task(self, orch, targets: tuple[str, ...]) -> HorizonTask:
        workspace = orch.workspace
        projects: set[str] = set()
        files: list[str] = []

        if targets == (".",):
            projects.update(workspace.projects)
            name = "workspace-all"
            title = "Workspace-wide task"
        else:
            for target in targets:
                if target in workspace.projects:
                    projects.add(target)
                    continue
                rel = Path(target)
                matched = self._project_for_path(workspace, rel)
                if matched is None:
                    log.error(f"Unknown run target {target!r}; expected a task name, project name, '.', '*', or project file path.")
                    raise typer.Exit(1)
                projects.add(matched)
                files.append(rel.as_posix())
            digest = hashlib.sha1("\n".join(targets).encode("utf-8")).hexdigest()[:10]
            name = f"adhoc-{digest}"
            title = "Ad hoc task"

        if not projects:
            log.error("No projects are configured, so no ad hoc task can be inferred.")
            raise typer.Exit(1)
        project_tuple = tuple(sorted(projects))
        task = HorizonTask(
            id=name,
            project=project_tuple[0],
            objective="",
            title=title,
            explanation="",
            projects=project_tuple,
            priority="normal",
            status=TaskStatus.QUEUED,
            write_set=WriteSet(projects=project_tuple, files=tuple(files)),
            scope=ItemScope(projects=project_tuple, files=tuple(files)),
            metadata={"ad_hoc": True, "run_targets": targets},
        )
        existing = {t.id: t for t in orch.task_store.list()}
        if name in existing:
            old = existing[name]
            task = dataclasses.replace(task, created_at=old.created_at)
        return orch.task_store.put(task)

    def _project_for_path(self, workspace, rel: Path) -> str | None:
        raw = rel.as_posix()
        for name, project in workspace.projects.items():
            prefix = project.path.as_posix().rstrip("/") + "/"
            if raw == project.path.as_posix() or raw.startswith(prefix):
                return name
        absolute = (self.root / rel).resolve()
        for name in workspace.projects:
            root = workspace.project_path(name).resolve()
            if absolute == root or root in absolute.parents:
                return name
        return None


def run(
    ctx: typer.Context,
    targets: list[str] = typer.Argument(None, help="Run target: '.', '*', 'ground', 'horizon', task names, project names, or files."),
    task: str | None = typer.Option(None, "--task", help="Pin one task name."),
    rounds: int | None = typer.Option(None, "--rounds", help="Override configured round count."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; do not run Horizon."),
    resume: str | None = typer.Option(
        None, "--resume",
        help="Resume an interrupted run from its last unfinished round: pass a run id (e.g. 0007), or 'latest' for the most recent.",
    ),
    backend: str = typer.Option(
        "default", "--backend",
        help="'default' streams a headless transcript (orchestrated). 'interactive' hands the terminal to the engine for a single role so you can type prompts — use with `ground` or `horizon`.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Run collaboration rounds.

    Targets: `.` (workspace), `*` (all projects), a task/project name or file, or a
    single role — `ground` (run one planning session) or `horizon` (run one prover
    session). Add `--backend interactive` to drive a role in a live terminal.
    """
    RunCommand(
        ctx.obj["root"],
        targets=tuple(targets or ()),
        task=task,
        rounds=rounds,
        dry_run=dry_run,
        resume=resume,
        backend=backend,
        as_json=as_json,
    ).run()
