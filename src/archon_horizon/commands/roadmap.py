"""Typer command group for roadmap artifacts."""

from __future__ import annotations

import typer

from archon_horizon.config.loader import build_stores
from archon_horizon.log import log
from archon_horizon.render.roadmap_md import render_roadmap_markdown

from .shared import load_workspace

app = typer.Typer(help="Render and inspect roadmaps.", no_args_is_help=True)


@app.command()
def render(ctx: typer.Context) -> None:
    """Render roadmap.yaml to reports/roadmap.md."""
    _, workspace = load_workspace(ctx.obj["root"])
    stores = build_stores(workspace)
    ref = stores.reports.write("roadmap", render_roadmap_markdown(stores.roadmap.load()))
    log.success(f"rendered {ref}")

