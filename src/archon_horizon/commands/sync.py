"""Typer-decorated ``sync`` entry point."""

from __future__ import annotations

import typer

from archon_horizon.log import log

from .shared import inbox_providers, load_workspace


def sync(ctx: typer.Context) -> None:
    """Sync inbox providers such as GitHub shadows."""
    cfg, workspace = load_workspace(ctx.obj["root"])
    _, providers = inbox_providers(cfg, workspace)
    rows: list[tuple[str, str, str]] = []
    for provider in providers:
        if "sync" not in provider.capabilities:
            continue
        result = provider.sync()
        status = "error" if result.errors else "ok"
        detail = f"imported {result.imported}, errors {list(result.errors) or '-'}"
        rows.append((result.provider, status, detail))
    log.results_table(rows, title="Sync")

