"""Typer command group for structural project operations."""

from __future__ import annotations

import typer

from archon_horizon.config import operations
from archon_horizon.config.loader import build_stores
from archon_horizon.log import log

from .shared import emit_json, load_workspace

app = typer.Typer(help="Manage workspace projects.", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _event_log(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return build_stores(workspace).events


@app.command()
def add(
    ctx: typer.Context,
    name: str,
    path: str,
    type: str = typer.Option("lean", "--type", help="Project type."),
    build: str | None = typer.Option(None, "--build", help="Build command, e.g. lake build."),
    as_json: bool = _JSON,
) -> None:
    """Add a project to config.yaml."""
    operations.add_project(ctx.obj["root"], name, path, type=type, build_command=build, event_log=_event_log(ctx))
    if as_json:
        emit_json({"action": "add", "name": name, "path": path, "type": type, "build": build})
        return
    log.success(f"added project {name}")


@app.command()
def archive(ctx: typer.Context, name: str, as_json: bool = _JSON) -> None:
    """Archive a project under .archon-horizon/archive/."""
    dest = operations.archive_project(ctx.obj["root"], name, event_log=_event_log(ctx))
    if as_json:
        emit_json({"action": "archive", "name": name, "dest": str(dest)})
        return
    log.success(f"archived {name} -> {dest}")


@app.command()
def remove(ctx: typer.Context, name: str, as_json: bool = _JSON) -> None:
    """Remove a project from config.yaml without deleting its files."""
    operations.remove_project(ctx.obj["root"], name, event_log=_event_log(ctx))
    if as_json:
        emit_json({"action": "remove", "name": name})
        return
    log.success(f"removed {name}")


@app.command()
def merge(ctx: typer.Context, dest: str, source: str, as_json: bool = _JSON) -> None:
    """Move source files into dest and remove source from config.yaml."""
    operations.merge_projects(ctx.obj["root"], dest, source, event_log=_event_log(ctx))
    if as_json:
        emit_json({"action": "merge", "dest": dest, "source": source})
        return
    log.success(f"merged {source} into {dest}")

