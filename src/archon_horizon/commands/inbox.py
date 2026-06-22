"""Typer command group for the local inbox."""

from __future__ import annotations

import typer

from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxStatus
from archon_horizon.core.labels import ARCHON_ACCEPT, ARCHON_PENDING, ARCHON_REJECTED
from archon_horizon.log import log

from .shared import load_workspace, local_inbox

app = typer.Typer(help="Manage the local inbox.", no_args_is_help=True)


def _inbox(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return local_inbox(workspace)


@app.command("list")
def list_items(ctx: typer.Context) -> None:
    """List local inbox items."""
    items = _inbox(ctx).list_items()
    if not items:
        log.info("(empty)")
        return
    rows = [
        (item.id, item.status.value, f"{item.kind.value} [{','.join(item.labels)}] {item.body[:80]}")
        for item in items
    ]
    log.results_table(rows, title="Local Inbox")


@app.command()
def add(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="Inbox item body."),
    kind: InboxKind = typer.Option(InboxKind.HINT, "--kind", help="Item kind."),
    pending: bool = typer.Option(False, "--pending", help="Mark as pending instead of accepted."),
) -> None:
    """Add a local inbox item."""
    labels = (ARCHON_PENDING,) if pending else (ARCHON_ACCEPT,)
    created = _inbox(ctx).create_item(InboxDraft(kind=kind, body=body, labels=labels))
    log.success(f"created {created.id}")


@app.command()
def label(ctx: typer.Context, id: str, labels: list[str] = typer.Argument(...)) -> None:
    """Replace labels on an inbox item."""
    _inbox(ctx).update_labels(id, labels)
    log.success(f"relabeled {id}")


@app.command()
def complete(ctx: typer.Context, id: str) -> None:
    """Mark an inbox item completed."""
    _inbox(ctx).update_status(id, InboxStatus.COMPLETED)
    log.success(f"completed {id}")


@app.command()
def reject(ctx: typer.Context, id: str) -> None:
    """Mark an inbox item rejected."""
    _inbox(ctx).update_labels(id, [ARCHON_REJECTED])
    log.success(f"rejected {id}")


@app.command()
def archive(ctx: typer.Context, id: str) -> None:
    """Archive an inbox item."""
    _inbox(ctx).update_status(id, InboxStatus.ARCHIVED)
    log.success(f"archived {id}")


@app.command()
def delete(ctx: typer.Context, id: str) -> None:
    """Delete an inbox item."""
    _inbox(ctx).delete_item(id)
    log.success(f"deleted {id}")

