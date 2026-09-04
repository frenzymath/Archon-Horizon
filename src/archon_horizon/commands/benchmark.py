"""Rank Lean files by static ``set_option`` heartbeat cost."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.lean.benchmark import benchmark_workspace
from archon_horizon.log import log

from .shared import emit_json, load_workspace

app = typer.Typer(
    help="Rank Lean files by set_option heartbeat budgets (elaboration cost signal).",
    invoke_without_command=True,
    no_args_is_help=False,
)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _project_roots(workspace, names: list[str] | None) -> dict[str, Path]:
    """Resolve project name → path for configured projects."""
    configured = dict(workspace.projects)
    if names:
        missing = [n for n in names if n not in configured]
        if missing:
            raise ValueError(
                "Unknown project(s): "
                + ", ".join(missing)
                + ". Configured: "
                + (", ".join(sorted(configured)) or "(none)")
            )
        selected = names
    else:
        selected = list(configured)
    return {name: workspace.project_path(name).resolve() for name in selected}


@app.callback(invoke_without_command=True)
def benchmark(
    ctx: typer.Context,
    project: list[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Limit to one or more projects (repeatable). Default: all configured projects.",
    ),
    min_heartbeats: int = typer.Option(
        1,
        "--min-heartbeats",
        min=0,
        help="Omit files whose summed heartbeat budget is below this.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        "-n",
        min=1,
        help="Show only the top N files after ranking.",
    ),
    details: bool = typer.Option(
        False,
        "--details",
        help="Include per-set_option line hits.",
    ),
    as_json: bool = _JSON,
) -> None:
    """List Lean files ordered by total heartbeat budget from set_option overrides.

    The indicator is the sum of numeric values from
    ``set_option maxHeartbeats``, ``set_option synthInstance.maxHeartbeats``,
    and related resource options. High ranks often mark layered API debt or
    candidates for a clean module rewrite (see the ``restart-module`` skill).
    """
    if ctx.invoked_subcommand is not None:
        return
    _, workspace = load_workspace(Path(ctx.obj["root"]))
    roots = _project_roots(workspace, list(project) if project else None)
    if not roots:
        raise ValueError("No projects configured. Add one with `horizon project add`.")
    payload = benchmark_workspace(
        roots,
        min_heartbeats=min_heartbeats,
        include_details=details,
        limit=limit,
    )
    if as_json:
        emit_json(payload)
        return
    files = payload["files"]
    if not files:
        log.info(
            "No Lean files with set_option heartbeat overrides"
            + (f" (min={min_heartbeats})" if min_heartbeats else "")
            + "."
        )
        return
    log.info(
        f"Heartbeat benchmark — {payload['total_files']} file(s), "
        f"Σ heartbeats={payload['total_heartbeats']:,}, "
        f"hits={payload['total_hits']}"
    )
    width_proj = max((len(str(f["project"])) for f in files), default=7)
    width_hb = max((len(f"{int(f['heartbeats']):,}") for f in files), default=10)
    for row in files:
        hb = f"{int(row['heartbeats']):,}".rjust(width_hb)
        proj = str(row["project"]).ljust(width_proj)
        hits = int(row["hits"])
        opts = ",".join(row.get("options") or []) or "-"
        log.step(
            f"{hb}  {proj}  {row['path']}  "
            f"({hits} hit{'s' if hits != 1 else ''}; {opts})"
        )
        if details:
            for hit in row.get("details") or []:
                log.info(f"      L{hit['line']}: {hit['option']} = {int(hit['value']):,}")
