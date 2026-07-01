"""Typer command group for the local inbox."""

from __future__ import annotations

import os

import typer

from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxScope, InboxStatus
from archon_horizon.core.labels import AGENT_READY, NOT_READY, REJECTED
from archon_horizon.log import log
from archon_horizon.store import serde

from .shared import emit_json, load_workspace, local_inbox, with_provenance

app = typer.Typer(help="Manage the local inbox.", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _agent_author(default: str | None = None) -> str | None:
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    if role in {"ground", "horizon"}:
        return role
    return default


def _inbox(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return local_inbox(workspace)


def _ensure_title_and_description(body: str) -> None:
    """An inbox item must be more than a title: a scannable title line AND a
    non-empty description. Rejects the title-only items the UI can't show well."""
    title, _, description = body.partition("\n\n")
    if not description.strip():  # tolerate a single newline between the two parts
        title, _, description = body.partition("\n")
    if not title.strip() or not description.strip():
        raise typer.BadParameter(
            "an inbox item needs a short title AND a description; pass both, e.g. "
            '--body "Short title\\n\\nDescription with details and next action."'
        )


def _item_dict(item) -> dict:
    return {
        "id": item.id,
        "provider": item.provider,
        "kind": item.kind.value,
        "status": item.status.value,
        "labels": list(item.labels),
        "body": item.body,
        "scope": serde.to_jsonable(item.scope),
        "audience": getattr(item, "audience", "") or "",
        "author": getattr(item, "author", "") or "",
        "source_ref": item.source_ref,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "comments": list(item.metadata.get("comments", [])),
        "metadata": {k: v for k, v in item.metadata.items() if k != "comments"},
    }


@app.command("list")
def list_items(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """List local inbox items."""
    items = _inbox(ctx).list_items()
    if as_json:
        emit_json({"items": [_item_dict(i) for i in items]})
        return
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
    project: str | None = typer.Option(None, "--project", help="Scope the item to a project (what it is ABOUT)."),
    to: str | None = typer.Option(
        None, "--to", help="Recipient the item is FOR: horizon | ground | human | project:<name>."
    ),
    author: str | None = typer.Option(None, "--author", help="Who is writing it (human / horizon / ground)."),
    persistent: bool = typer.Option(False, "--persistent", help="Tag a standing hint the agent never closes."),
    temporary: bool = typer.Option(False, "--temporary", help="Tag a one-shot hint the agent closes once used."),
    pending: bool = typer.Option(False, "--pending", help="Mark not-ready (withheld from the agents) instead of agent-ready."),
    as_json: bool = _JSON,
) -> None:
    """Add a local inbox item.

    ``--persistent``/``--temporary`` are sugar: they prepend a ``[persistent]`` or
    ``[temporary]`` tag to the body, which is the only signal agents read to decide
    whether to keep or close the item. ``--to`` addresses the item to a recipient
    (e.g. ``--to project:foo`` to message another project).
    """
    if persistent and temporary:
        raise typer.BadParameter("use at most one of --persistent / --temporary")
    _ensure_title_and_description(body)
    if persistent:
        body = f"[persistent] {body}"
    elif temporary:
        body = f"[temporary] {body}"
    labels = (NOT_READY,) if pending else (AGENT_READY,)
    scope = InboxScope(projects=(project,) if project else ())
    created = _inbox(ctx).create_item(
        InboxDraft(kind=kind, body=body, labels=labels, scope=scope, audience=(to or ""),
                   author=_agent_author("human") if author is None else author,
                   metadata=with_provenance())
    )
    if as_json:
        emit_json(_item_dict(created))
        return
    log.success(f"created {created.id}")


@app.command(hidden=True)
def comment(
    ctx: typer.Context,
    id: str,
    body: str = typer.Option(..., "--body", help="Comment body."),
    author: str | None = typer.Option(None, "--author", help="Comment author."),
    as_json: bool = _JSON,
) -> None:
    """Add a progress comment to an inbox item (track work, not ask the human)."""
    _inbox(ctx).add_comment(id, body, author=author or _agent_author())
    if as_json:
        emit_json({"id": id, "commented": True})
        return
    log.success(f"commented on {id}")


@app.command()
def protect(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="The constraint, e.g. 'do not change the signature of Foo.bar'."),
    project: str | None = typer.Option(None, "--project", help="Restrict to a project."),
    file: str | None = typer.Option(None, "--file", help="Restrict to a file."),
    declaration: str | None = typer.Option(None, "--declaration", help="Restrict to a declaration."),
    as_json: bool = _JSON,
) -> None:
    """Add a standing protection the Horizon agent must respect (the soft freeze).

    Protections are persistent and may be semantic (e.g. a signature). They are
    rendered to the agents as do-not-modify constraints, not hard-enforced.
    """
    scope = InboxScope(
        projects=(project,) if project else (),
        files=(file,) if file else (),
        declarations=(declaration,) if declaration else (),
    )
    created = _inbox(ctx).create_item(
        InboxDraft(
            kind=InboxKind.PROTECTION,
            body=f"[persistent] {body}",
            labels=(AGENT_READY,),
            scope=scope,
            audience="horizon",
            author=_agent_author("human") or "human",
            metadata=with_provenance(),
        )
    )
    if as_json:
        emit_json(_item_dict(created))
        return
    log.success(f"protected {created.id}")


@app.command()
def edit(
    ctx: typer.Context,
    id: str,
    body: str | None = typer.Option(None, "--body", help="Replacement item body."),
    kind: InboxKind | None = typer.Option(None, "--kind", help="Replacement item kind."),
    as_json: bool = _JSON,
) -> None:
    """Edit the body and/or kind of a local inbox item."""
    if body is None and kind is None:
        raise typer.BadParameter("provide --body and/or --kind")
    inbox = _inbox(ctx)
    if body is not None:
        inbox.update_body(id, body, _agent_author())
    if kind is not None:
        inbox.update_kind(id, kind, _agent_author())
    if as_json:
        emit_json({"id": id, "edited": True})
        return
    log.success(f"edited {id}")


@app.command("edit-comment", hidden=True)
def edit_comment(
    ctx: typer.Context,
    id: str,
    index: int = typer.Option(..., "--index", help="Zero-based comment index."),
    body: str = typer.Option(..., "--body", help="Replacement comment body."),
    author: str | None = typer.Option(None, "--author", help="Replacement author."),
    as_json: bool = _JSON,
) -> None:
    """Edit a local inbox comment."""
    _inbox(ctx).update_comment(id, index, body, author=author)
    if as_json:
        emit_json({"id": id, "index": index, "edited": True})
        return
    log.success(f"edited comment {index} on {id}")


@app.command(hidden=True)
def label(
    ctx: typer.Context,
    id: str,
    labels: list[str] = typer.Argument(...),
    as_json: bool = _JSON,
) -> None:
    """Replace labels on an inbox item."""
    _inbox(ctx).update_labels(id, labels, _agent_author())
    if as_json:
        emit_json({"id": id, "labels": list(labels)})
        return
    log.success(f"relabeled {id}")


@app.command()
def complete(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Mark an inbox item completed."""
    _inbox(ctx).update_status(id, InboxStatus.CLOSED, _agent_author())
    if as_json:
        emit_json({"id": id, "status": InboxStatus.CLOSED.value})
        return
    log.success(f"completed {id}")


@app.command(hidden=True)
def reject(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Mark an inbox item rejected."""
    _inbox(ctx).update_labels(id, [REJECTED], _agent_author())
    if as_json:
        emit_json({"id": id, "rejected": True})
        return
    log.success(f"rejected {id}")


@app.command()
def delete(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Delete an inbox item."""
    _inbox(ctx).delete_item(id)
    if as_json:
        emit_json({"id": id, "deleted": True})
        return
    log.success(f"deleted {id}")
