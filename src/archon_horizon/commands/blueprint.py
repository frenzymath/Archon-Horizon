"""Typer-decorated ``blueprint`` entry point."""

from __future__ import annotations

import json

import typer

from archon_horizon.blueprint.workspace import workspace_dags_rich
from archon_horizon.log import log

from .shared import emit_json, load_workspace


class BlueprintCommand:
    def __init__(self, ctx: typer.Context, *, as_json: bool = False) -> None:
        self.root = ctx.obj["root"]
        self.as_json = as_json

    def run(self) -> None:
        _, workspace = load_workspace(self.root)
        dags = workspace_dags_rich(workspace)
        if not dags:
            if self.as_json:
                emit_json({"projects": []})
            else:
                log.info("no parseable blueprints found")
            return
        out_dir = workspace.state_path / "blueprints"
        out_dir.mkdir(parents=True, exist_ok=True)
        rows: list[tuple[str, str, str]] = []
        summary: list[dict] = []
        for project, dag in dags.items():
            path = out_dir / f"{project}.json"
            path.write_text(json.dumps(dag, indent=2), "utf-8")
            summary.append({
                "project": project,
                "nodes": len(dag["nodes"]),
                "edges": len(dag["edges"]),
                "dangling": len(dag["dangling"]),
                "path": str(path),
            })
            rows.append((
                project,
                "ok",
                f"{len(dag['nodes'])} nodes, {len(dag['edges'])} edges, {len(dag['dangling'])} dangling -> {path}",
            ))
        if self.as_json:
            emit_json({"projects": summary})
            return
        log.results_table(rows, title="Blueprint DAGs")


def blueprint(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Parse project blueprints and write DAG JSON."""
    BlueprintCommand(ctx, as_json=as_json).run()

