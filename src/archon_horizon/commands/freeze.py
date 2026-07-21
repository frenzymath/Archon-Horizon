"""Manage enforced, config-backed workspace freeze rules."""

from __future__ import annotations

import typer

from archon_horizon.config import operations
from archon_horizon.config.loader import build_stores
from archon_horizon.log import log

from .shared import emit_json, load_workspace

app = typer.Typer(
    help="Manage enforced file, declaration, blueprint-node, project, and agent freezes.",
    no_args_is_help=True,
)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")
_KIND_HELP = "agent | project | file | declaration | blueprint-node"


def _event_log(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return build_stores(workspace).events


@app.command("list")
def list_rules(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """List enforced freeze rules from config.yaml."""
    rules = operations.list_freezes(ctx.obj["root"])
    if as_json:
        emit_json({"rules": {kind: list(patterns) for kind, patterns in rules.items()}})
        return
    rows = [(kind, "frozen", pattern) for kind, patterns in rules.items() for pattern in patterns]
    if rows:
        log.results_table(rows, title="Enforced freezes")
    else:
        log.info("No enforced freeze rules.")


@app.command()
def add(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help=_KIND_HELP),
    pattern: str = typer.Argument(..., help="Exact target or glob pattern to freeze."),
    as_json: bool = _JSON,
) -> None:
    """Add a freeze rule enforced before agent dispatch."""
    try:
        changed = operations.add_freeze(
            ctx.obj["root"], kind, pattern, event_log=_event_log(ctx)
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="kind/pattern") from exc
    if as_json:
        emit_json({"action": "add", "kind": kind, "pattern": pattern, "changed": changed})
        return
    log.success(f"{'froze' if changed else 'already frozen'} {kind} {pattern}")


@app.command()
def remove(
    ctx: typer.Context,
    kind: str = typer.Argument(..., help=_KIND_HELP),
    pattern: str = typer.Argument(..., help="Exact target or glob pattern to unfreeze."),
    as_json: bool = _JSON,
) -> None:
    """Remove an enforced freeze rule from config.yaml."""
    try:
        changed = operations.remove_freeze(
            ctx.obj["root"], kind, pattern, event_log=_event_log(ctx)
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="kind/pattern") from exc
    if as_json:
        emit_json({"action": "remove", "kind": kind, "pattern": pattern, "changed": changed})
        return
    if changed:
        log.success(f"unfroze {kind} {pattern}")
    else:
        log.info(f"no matching freeze for {kind} {pattern}")
