"""Typer-decorated ``blueprint`` entry point."""

from __future__ import annotations

import json

import typer

from archon_horizon.blueprint.workspace import workspace_dags
from archon_horizon.log import log

from .shared import load_workspace


class BlueprintCommand:
    def __init__(self, ctx: typer.Context) -> None:
        self.root = ctx.obj["root"]

    def run(self) -> None:
        _, workspace = load_workspace(self.root)
        dags = workspace_dags(workspace)
        if not dags:
            log.info("no parseable blueprints found")
            return
        out_dir = workspace.state_path / "blueprints"
        out_dir.mkdir(parents=True, exist_ok=True)
        rows: list[tuple[str, str, str]] = []
        for project, dag in dags.items():
            path = out_dir / f"{project}.json"
            path.write_text(json.dumps(dag, indent=2), "utf-8")
            rows.append((
                project,
                "ok",
                f"{len(dag['nodes'])} nodes, {len(dag['edges'])} edges, {len(dag['dangling'])} dangling -> {path}",
            ))
        log.results_table(rows, title="Blueprint DAGs")


def blueprint(ctx: typer.Context) -> None:
    """Parse project blueprints and write DAG JSON."""
    BlueprintCommand(ctx).run()

