"""Workspace-local scratch inspection and conservative cleanup."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.core.scratch import clean_workspace_tmp
from archon_horizon.log import log

from .shared import emit_json, load_workspace

app = typer.Typer(
    help="Inspect and safely reclaim workspace-local temporary files.",
    no_args_is_help=True,
)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


@app.command("path")
def path(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """Print the workspace-local scratch root used by agent subprocesses."""

    _, workspace = load_workspace(Path(ctx.obj["root"]))
    scratch = workspace.tmp_path
    scratch.mkdir(parents=True, exist_ok=True)
    if as_json:
        emit_json({"path": str(scratch.resolve())})
        return
    log.info(str(scratch.resolve()))


@app.command("clean")
def clean(
    ctx: typer.Context,
    older_than_hours: float = typer.Option(
        24.0,
        "--older-than-hours",
        min=0.0,
        help="Only consider entries whose newest file is this old (default: 24).",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Actually remove candidates. Without this flag the command is a dry run.",
    ),
    as_json: bool = _JSON,
) -> None:
    """List or remove stale scratch entries, protecting live runs."""

    _, workspace = load_workspace(Path(ctx.obj["root"]))
    result = clean_workspace_tmp(
        workspace.state_path,
        older_than_s=older_than_hours * 60 * 60,
        apply=apply,
    )
    payload = {
        "path": str(workspace.tmp_path.resolve()),
        "dry_run": not apply,
        "older_than_hours": older_than_hours,
        "candidates": list(result.candidates),
        "removed": list(result.removed),
        "skipped_live_runs": list(result.skipped_live_runs),
    }
    if as_json:
        emit_json(payload)
        return
    if apply:
        noun = "entry" if len(result.removed) == 1 else "entries"
        log.success(f"Removed {len(result.removed)} stale scratch {noun}.")
    else:
        noun = "entry" if len(result.candidates) == 1 else "entries"
        log.info(f"Dry run: {len(result.candidates)} stale scratch {noun} would be removed.")
    if result.skipped_live_runs:
        log.info("Protected live run scratch: " + ", ".join(result.skipped_live_runs))
    for item in result.candidates if not apply else result.removed:
        log.step(item)

