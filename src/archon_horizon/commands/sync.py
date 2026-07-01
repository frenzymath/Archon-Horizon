"""Typer-decorated ``sync`` entry point."""

from __future__ import annotations

import typer

from archon_horizon.log import log

from .shared import emit_json, inbox_providers, load_workspace


def sync(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Sync inbox providers such as GitHub shadows."""
    cfg, workspace = load_workspace(ctx.obj["root"])
    _, providers = inbox_providers(cfg, workspace)
    rows: list[tuple[str, str, str]] = []
    summary: list[dict] = []
    for provider in providers:
        if "sync" not in provider.capabilities:
            continue
        result = provider.sync()
        summary.append({
            "provider": result.provider,
            "imported": result.imported,
            "errors": list(result.errors),
        })
        status = "error" if result.errors else "ok"
        detail = f"imported {result.imported}, errors {list(result.errors) or '-'}"
        rows.append((result.provider, status, detail))
    if as_json:
        emit_json({"providers": summary})
        return
    log.results_table(rows, title="Sync")

