"""Typer-decorated ``run`` entry point."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.config.harnesses import HarnessRegistry
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.log import log

from .shared import inbox_providers, load_workspace


class RunCommand:
    def __init__(
        self,
        root: Path,
        *,
        projects: tuple[str, ...] = (),
        task: str | None = None,
        proposal: str | None = None,
        rounds: int | None = None,
        dry_run: bool = False,
    ) -> None:
        self.root = root
        self.projects = projects
        self.task = task
        self.proposal = proposal
        self.rounds = rounds
        self.dry_run = dry_run

    def run(self) -> None:
        cfg, workspace = load_workspace(self.root)
        _, providers = inbox_providers(cfg, workspace)
        orch = build_orchestrator(self.root, registry=HarnessRegistry(), inbox_providers=providers)
        focus = Focus(projects=self.projects, task=self.task, proposal=self.proposal)
        run = RunRecord(id="", focus=focus, rounds_requested=self.rounds or cfg.rounds)
        reports = orch.run(run, dry_run=self.dry_run)
        rows: list[tuple[str, str, str]] = []
        for report in reports:
            if self.dry_run:
                rows.append((f"round {report.round_index}", "planned", ", ".join(report.planned) or "-"))
            else:
                detail = f"ran {', '.join(report.tasks_run) or '-'}; blocked {', '.join(report.tasks_blocked) or '-'}"
                rows.append((f"round {report.round_index}", "done", detail))
        log.results_table(rows, title="Run")


def run(
    ctx: typer.Context,
    projects: list[str] = typer.Argument(None, help="Optional focus projects."),
    task: str | None = typer.Option(None, "--task", help="Pin one task id."),
    proposal: str | None = typer.Option(None, "--proposal", help="Apply a proposal before running."),
    rounds: int | None = typer.Option(None, "--rounds", help="Override configured round count."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan only; do not run Horizon."),
) -> None:
    """Run collaboration rounds."""
    RunCommand(
        ctx.obj["root"],
        projects=tuple(projects or ()),
        task=task,
        proposal=proposal,
        rounds=rounds,
        dry_run=dry_run,
    ).run()

