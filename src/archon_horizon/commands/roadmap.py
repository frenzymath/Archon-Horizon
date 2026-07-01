"""Typer command group for the sharded roadmap."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import typer

from archon_horizon.core.clock import utc_now
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapKind, RoadmapStatus
from archon_horizon.core.scope import ItemScope
from archon_horizon.log import log
from archon_horizon.store import serde

from .shared import agent_author, emit_json, history_entry as _history_entry, load_workspace, roadmap_store, with_provenance

app = typer.Typer(help="Read and update the roadmap.", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _store(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return roadmap_store(workspace)


def _item_dict(item: RoadmapItem) -> dict:
    return {
        "id": item.id,
        "title": item.title,
        "projects": list(item.projects),
        "summary": item.summary,
        "status": item.status.value,
        "kind": item.kind.value,
        "priority": item.priority,
        "depends_on": list(item.depends_on),
        "inbox_refs": list(item.inbox_refs),
        "scope": serde.to_jsonable(item.scope),
    }


def _summary_text(summary: str | None, summary_file: str | None) -> str | None:
    """Resolve the summary from a literal string or a file (for long prose)."""
    if summary_file:
        return Path(summary_file).read_text("utf-8")
    return summary


def _save(store, items: tuple[RoadmapItem, ...]) -> None:
    store.save(Roadmap(items=items, updated_at=utc_now()))


@app.command("list")
def list_items(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """List roadmap items."""
    items = _store(ctx).load().items
    if as_json:
        emit_json({"items": [_item_dict(i) for i in items]})
        return
    if not items:
        log.info("Roadmap is empty.")
        return
    log.results_table(
        [(i.id, i.status.value, i.title) for i in items],
        title="Roadmap",
    )


@app.command("set")
def set_item(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="Roadmap item id, e.g. A.3."),
    status: str | None = typer.Option(None, "--status", help="active|pending|blocked|done|rejected."),
    summary: str | None = typer.Option(None, "--summary", help="Replace the summary (any text — quoted safely)."),
    summary_file: str | None = typer.Option(None, "--summary-file", help="Read the summary from a file (for long prose)."),
    title: str | None = typer.Option(None, "--title", help="Replace the title."),
    priority: str | None = typer.Option(None, "--priority", help="urgent|high|normal|low."),
    kind: str | None = typer.Option(None, "--kind", help="proof|blueprint|refactor|workspace|report."),
    author: str | None = typer.Option(None, "--author", help="Who is making the change (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Update fields of an existing roadmap item."""
    store = _store(ctx)
    items = list(store.load().items)
    idx = next((n for n, it in enumerate(items) if it.id == item_id), None)
    if idx is None:
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    item = items[idx]
    actor = author or agent_author() or item.metadata.get("author")
    changes: dict[str, object] = {}
    if status is not None:
        changes["status"] = RoadmapStatus(status.lower())
        store.append_history(item_id, _history_entry(actor, "status",
                             before=str(item.status.value), after=status.lower()))
    if kind is not None:
        changes["kind"] = RoadmapKind(kind.lower())
    new_summary = _summary_text(summary, summary_file)
    if new_summary is not None:
        changes["summary"] = new_summary
    if title is not None:
        changes["title"] = title
    if priority is not None:
        changes["priority"] = priority
    edited_fields = [f for f in changes if f != "status"]
    if edited_fields:
        store.append_history(item_id, _history_entry(actor, "edited",
                             note=", ".join(edited_fields) + " updated"))
    metadata = {**item.metadata, "updated_at": utc_now().isoformat()}
    items[idx] = dataclasses.replace(item, metadata=metadata, **changes)
    _save(store, tuple(items))
    if as_json:
        emit_json(_item_dict(items[idx]))
        return
    log.success(f"Updated roadmap item {item_id} ({', '.join(changes) or 'no fields'}).")


@app.command("add")
def add_item(
    ctx: typer.Context,
    item_id: str = typer.Option(..., "--id", help="New item id, e.g. A.4."),
    title: str = typer.Option(..., "--title", help="Item title."),
    projects: list[str] = typer.Option(..., "--project", help="Project the item targets (repeatable)."),
    summary: str | None = typer.Option(None, "--summary"),
    summary_file: str | None = typer.Option(None, "--summary-file"),
    status: str = typer.Option("pending", "--status"),
    kind: str = typer.Option("proof", "--kind"),
    priority: str = typer.Option("normal", "--priority"),
    author: str | None = typer.Option(None, "--author", help="Who is adding the item (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Add a new roadmap item."""
    store = _store(ctx)
    items = list(store.load().items)
    if any(it.id == item_id for it in items):
        log.error(f"Roadmap item {item_id!r} already exists; use `roadmap set`.")
        raise typer.Exit(1)
    actor = author or agent_author("human")
    item = RoadmapItem(
        id=item_id,
        title=title,
        projects=tuple(projects),
        summary=_summary_text(summary, summary_file) or "",
        status=RoadmapStatus(status.lower()),
        kind=RoadmapKind(kind.lower()),
        priority=priority,
        scope=ItemScope(projects=tuple(projects)),
        metadata=with_provenance({"author": actor, "created_at": utc_now().isoformat()}),
    )
    items.append(item)
    _save(store, tuple(items))
    store.append_history(item_id, _history_entry(actor, "created", after=item.status.value, note="opened"))
    if as_json:
        emit_json(_item_dict(item))
        return
    log.success(f"Added roadmap item {item_id}.")


@app.command("comment")
def comment_item(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="Roadmap item id."),
    body: str = typer.Option(..., "--body", help="Comment body (Markdown)."),
    author: str | None = typer.Option(None, "--author", help="Comment author (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Add a progress comment to a roadmap item (record a key advance)."""
    store = _store(ctx)
    if not any(it.id == item_id for it in store.load().items):
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    store.add_comment(item_id, body, author or agent_author())
    if as_json:
        emit_json({"id": item_id, "commented": True})
        return
    log.success(f"Commented on roadmap item {item_id}.")


@app.command("remove")
def remove_item(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="Roadmap item id to remove."),
) -> None:
    """Remove a roadmap item."""
    store = _store(ctx)
    items = [it for it in store.load().items if it.id != item_id]
    if len(items) == len(store.load().items):
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    _save(store, tuple(items))
    log.success(f"Removed roadmap item {item_id}.")
