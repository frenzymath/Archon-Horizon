"""Typer command group for the sharded roadmap."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import typer

from archon_horizon.core.collection_health import roadmap_health_warnings
from archon_horizon.core.clock import utc_now
from archon_horizon.core.roadmap import (
    Roadmap,
    RoadmapItem,
    RoadmapKind,
    RoadmapStatus,
    apply_hierarchy,
    hierarchy_status_warnings,
    item_depth,
    item_milestone,
    item_owner,
    item_parent,
    item_pinned_commits,
    ordered_tree,
    subtree,
    subtree_progress,
)
from archon_horizon.core.scope import ItemScope
from archon_horizon.log import log
from archon_horizon.store import serde

from .shared import agent_author, emit_json, ensure_concise_agent_message, history_entry as _history_entry, load_workspace, provenance_project, roadmap_store, with_provenance

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
        "task_refs": list(item.task_refs),
        "scope": serde.to_jsonable(item.scope),
        "parent": item_parent(item),
        "depth": item_depth(item),
        "owner": item_owner(item),
        "milestone": item_milestone(item),
        "pinned_commits": list(item_pinned_commits(item)),
    }


def _normalize_id_list(values: list[str] | None) -> list[str] | None:
    """``None`` means untouched; otherwise de-dupe, strip, drop empties (order kept)."""
    if values is None:
        return None
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        for part in str(raw).split(","):
            value = part.strip()
            if value and value not in seen:
                seen.add(value)
                out.append(value)
    return out


def _rewrite_id_refs(items: list[RoadmapItem], old_id: str, new_id: str) -> list[RoadmapItem]:
    """Rewrite parent / depends_on / nested metadata links that pointed at ``old_id``."""
    rewritten: list[RoadmapItem] = []
    for item in items:
        meta = dict(item.metadata)
        parent = str(meta.get("parent") or "").strip()
        if parent == old_id:
            meta["parent"] = new_id
        depends = tuple(new_id if dep == old_id else dep for dep in item.depends_on)
        rewritten.append(
            dataclasses.replace(item, depends_on=depends, metadata=meta)
            if depends != item.depends_on or meta != item.metadata
            else item
        )
    return rewritten


def _apply_board_meta(
    metadata: dict,
    *,
    owner: str | None = None,
    milestone: str | None = None,
    pin: tuple[str, ...] = (),
    unpin: tuple[str, ...] = (),
) -> dict:
    """Fold board fields into an item's metadata. ``owner``/``milestone`` are only
    touched when not ``None``; an empty string clears them. ``pin``/``unpin`` add
    or remove commit SHAs (newest pinned first, de-duplicated)."""
    meta = dict(metadata)
    for key, value in (("owner", owner), ("milestone", milestone)):
        if value is not None:
            stripped = value.strip()
            if stripped:
                meta[key] = stripped
            else:
                meta.pop(key, None)
    if pin or unpin:
        current = [str(s).strip() for s in meta.get("pinned_commits", []) if str(s).strip()]
        for sha in pin:
            sha = sha.strip()
            if sha and sha not in current:
                current.insert(0, sha)
        drop = {s.strip() for s in unpin}
        current = [s for s in current if s not in drop]
        if current:
            meta["pinned_commits"] = current
        else:
            meta.pop("pinned_commits", None)
    return meta


def _hierarchy_meta(base: dict, depth: int | None, parent: str | None) -> dict:
    """Fold ``--depth`` / ``--parent`` into an item's metadata (clearing a parent
    when passed the empty string, so an item can be un-nested)."""
    return apply_hierarchy(base, depth=depth, parent=parent)


def _summary_text(summary: str | None, summary_file: str | None) -> str | None:
    """Resolve the summary from a literal string or a file (for long prose)."""
    if summary_file:
        return Path(summary_file).read_text("utf-8")
    return summary


def _save(store, items: tuple[RoadmapItem, ...]) -> None:
    store.save(Roadmap(items=items, updated_at=utc_now()))


def _roadmap_warnings(items) -> list[str]:
    """All advisory roadmap warnings, with no automatic state changes."""
    return [*hierarchy_status_warnings(items), *roadmap_health_warnings(items)]


def _warn_roadmap(items) -> list[str]:
    """Surface roadmap advisories; the editor decides whether to act."""
    warnings = _roadmap_warnings(items)
    for warning in warnings:
        log.warn(warning)
    return warnings


def _validate_parent(items: list[RoadmapItem], item_id: str, parent: str) -> None:
    """A parent must exist and not be the item itself (a cycle). Unknown/self
    parents are hard errors so a typo doesn't silently detach the item."""
    if parent == item_id:
        log.error(f"An item cannot be its own parent ({item_id}).")
        raise typer.Exit(1)
    if not any(it.id == parent for it in items):
        log.error(f"Parent item {parent!r} does not exist; add it first or fix the id.")
        raise typer.Exit(1)


@app.command("list")
def list_items(
    ctx: typer.Context,
    focus: str | None = typer.Option(None, "--focus", help="Show only this item and its descendants (its subtree)."),
    max_depth: int | None = typer.Option(None, "--max-depth", help="Hide items deeper than this level (0 = top-level only)."),
    milestone: str | None = typer.Option(None, "--milestone", help="Only items in this milestone label."),
    owner: str | None = typer.Option(None, "--owner", help="Only items owned by this team/agent."),
    as_json: bool = _JSON,
) -> None:
    """List roadmap items as an indented outline (parents above their sub-items)."""
    items = _store(ctx).load().items
    rows = subtree(items, focus) if focus else ordered_tree(items)
    if max_depth is not None:
        rows = [(it, d) for it, d in rows if d <= max_depth]
    if milestone is not None:
        rows = [(it, d) for it, d in rows if item_milestone(it) == milestone]
    if owner is not None:
        rows = [(it, d) for it, d in rows if item_owner(it) == owner]
    progress = subtree_progress(items)
    if as_json:
        emit_json({
            "items": [
                {
                    **_item_dict(it),
                    "tree_depth": d,
                    **({"subtree_done": progress[it.id][0], "subtree_total": progress[it.id][1]}
                       if it.id in progress else {}),
                }
                for it, d in rows
            ],
            "warnings": _roadmap_warnings(items),
        })
        return
    if not items:
        log.info("Roadmap is empty.")
        return
    if focus and not rows:
        log.error(f"No roadmap item {focus!r}.")
        raise typer.Exit(1)

    def _status_cell(item: RoadmapItem) -> str:
        # Parents show subtree progress at a glance — the roadmap is the agents'
        # strategy sketch, so "active · 3/7 done" reads as a plan, not a flat list.
        if item.id in progress:
            done, total = progress[item.id]
            return f"{item.status.value} · {done}/{total} done"
        return item.status.value

    def _title_cell(item: RoadmapItem, depth: int) -> str:
        tags = []
        if item_milestone(item):
            tags.append(f"◇{item_milestone(item)}")
        if item_owner(item):
            tags.append(f"@{item_owner(item)}")
        suffix = f"  [{' '.join(tags)}]" if tags else ""
        return "  " * depth + item.title + suffix

    log.results_table(
        [("  " * d + i.id, _status_cell(i), _title_cell(i, d)) for i, d in rows],
        title="Roadmap" + (f" · {focus} subtree" if focus else ""),
    )
    _warn_roadmap(items)


@app.command("show")
def show_item(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="Roadmap item id."),
    as_json: bool = _JSON,
) -> None:
    """Show one roadmap item in full (fields, hierarchy, board metadata)."""
    store = _store(ctx)
    items = store.load().items
    item = next((it for it in items if it.id == item_id), None)
    if item is None:
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    payload = _item_dict(item)
    progress = subtree_progress(items)
    if item_id in progress:
        done, total = progress[item_id]
        payload["subtree_done"] = done
        payload["subtree_total"] = total
    warnings = _roadmap_warnings(items)
    if as_json:
        emit_json({**payload, **({"warnings": warnings} if warnings else {})})
        return
    log.info(str(payload))
    if item.summary:
        log.panel(item.summary, title="summary")
    _warn_roadmap(items)


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
    parent: str | None = typer.Option(None, "--parent", help="Nest under this item id; pass '' to un-nest to top level."),
    depth: int | None = typer.Option(None, "--depth", help="Indentation level when there is no --parent (0 = top level)."),
    owner: str | None = typer.Option(None, "--owner", help="Team/agent responsible; pass '' to clear."),
    milestone: str | None = typer.Option(None, "--milestone", help="Milestone label for grouping/filtering; pass '' to clear."),
    project: list[str] | None = typer.Option(None, "--project", help="Replace the item's projects (repeatable; omit to leave unchanged)."),
    depends_on: list[str] | None = typer.Option(None, "--depends-on", help="Replace depends_on ids (repeatable; pass '' alone to clear)."),
    inbox_ref: list[str] | None = typer.Option(None, "--inbox-ref", help="Replace inbox_refs (repeatable; pass '' alone to clear)."),
    task_ref: list[str] | None = typer.Option(None, "--task-ref", help="Replace task_refs (repeatable; pass '' alone to clear)."),
    pin_commit: list[str] = typer.Option((), "--pin-commit", help="Pin a commit SHA as a deliverable (repeatable)."),
    unpin_commit: list[str] = typer.Option((), "--unpin-commit", help="Remove a pinned commit SHA (repeatable)."),
    author: str | None = typer.Option(None, "--author", help="Who is making the change (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Update fields of an existing roadmap item.

    Nesting (``--parent`` / ``--depth``), board metadata (``--owner`` /
    ``--milestone`` / pin commits), dependencies, projects, and refs are all
    editable here so agents can reshape the long-term plan without hand-editing
    YAML. Empty-string values clear optional fields (parent, owner, milestone,
    depends-on, refs)."""
    store = _store(ctx)
    items = list(store.load().items)
    idx = next((n for n, it in enumerate(items) if it.id == item_id), None)
    if idx is None:
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    item = items[idx]
    actor = author or agent_author() or item.metadata.get("author")
    if parent and parent.strip():
        _validate_parent(items, item_id, parent.strip())
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
    projects = _normalize_id_list(project)
    if projects is not None:
        if not projects:
            log.error("A roadmap item needs at least one --project.")
            raise typer.Exit(1)
        changes["projects"] = tuple(projects)
        changes["scope"] = dataclasses.replace(item.scope, projects=tuple(projects))
    depends = _normalize_id_list(depends_on)
    if depends is not None:
        unknown = [dep for dep in depends if dep != item_id and not any(it.id == dep for it in items)]
        if unknown:
            log.error(f"Unknown depends-on id(s): {', '.join(unknown)}.")
            raise typer.Exit(1)
        if item_id in depends:
            log.error("An item cannot depend on itself.")
            raise typer.Exit(1)
        changes["depends_on"] = tuple(depends)
    inbox_refs = _normalize_id_list(inbox_ref)
    if inbox_refs is not None:
        changes["inbox_refs"] = tuple(inbox_refs)
    task_refs = _normalize_id_list(task_ref)
    if task_refs is not None:
        changes["task_refs"] = tuple(task_refs)
    if parent is not None or depth is not None:
        store.append_history(item_id, _history_entry(actor, "nested",
                             note=(f"parent={parent.strip() or 'none'}" if parent is not None else f"depth={depth}")))
    board_fields = []
    if owner is not None:
        board_fields.append("owner")
    if milestone is not None:
        board_fields.append("milestone")
    if pin_commit or unpin_commit:
        board_fields.append("pinned_commits")
    if projects is not None:
        board_fields.append("projects")
    if depends is not None:
        board_fields.append("depends_on")
    if inbox_refs is not None:
        board_fields.append("inbox_refs")
    if task_refs is not None:
        board_fields.append("task_refs")
    edited_fields: list[str] = []
    for field in [*(f for f in changes if f not in {"status", "scope"}), *board_fields]:
        if field not in edited_fields:
            edited_fields.append(field)
    if edited_fields:
        store.append_history(item_id, _history_entry(actor, "edited",
                             note=", ".join(edited_fields) + " updated"))
    metadata = _hierarchy_meta(
        {**item.metadata, "updated_at": utc_now().isoformat()}, depth, parent
    )
    metadata = _apply_board_meta(
        metadata, owner=owner, milestone=milestone,
        pin=tuple(pin_commit), unpin=tuple(unpin_commit),
    )
    items[idx] = dataclasses.replace(item, metadata=metadata, **changes)
    _save(store, tuple(items))
    if as_json:
        emit_json({**_item_dict(items[idx]), "warnings": _roadmap_warnings(items)})
        return
    touched: list[str] = []
    for field in (*changes, *board_fields):
        if field == "scope" or field in touched:
            continue
        touched.append(field)
    log.success(f"Updated roadmap item {item_id} ({', '.join(touched) or 'no fields'}).")
    _warn_roadmap(items)


@app.command("add")
def add_item(
    ctx: typer.Context,
    item_id: str = typer.Option(..., "--id", help="New item id, e.g. A.4."),
    title: str = typer.Option(..., "--title", help="Item title."),
    projects: list[str] = typer.Option((), "--project", help="Project the item targets (repeatable); defaults to the session's project."),
    summary: str | None = typer.Option(None, "--summary"),
    summary_file: str | None = typer.Option(None, "--summary-file"),
    status: str = typer.Option("pending", "--status"),
    kind: str = typer.Option("proof", "--kind"),
    priority: str = typer.Option("normal", "--priority"),
    parent: str | None = typer.Option(None, "--parent", help="Nest the new item under this existing item id."),
    depth: int | None = typer.Option(None, "--depth", help="Indentation level when there is no --parent (0 = top level)."),
    owner: str | None = typer.Option(None, "--owner", help="Team/agent responsible."),
    milestone: str | None = typer.Option(None, "--milestone", help="Milestone label for grouping/filtering."),
    depends_on: list[str] = typer.Option((), "--depends-on", help="Prerequisite roadmap item id(s) (repeatable)."),
    inbox_ref: list[str] = typer.Option((), "--inbox-ref", help="Linked inbox item id(s) (repeatable)."),
    task_ref: list[str] = typer.Option((), "--task-ref", help="Linked task id(s) (repeatable)."),
    author: str | None = typer.Option(None, "--author", help="Who is adding the item (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Add a new roadmap item (optionally nested under a --parent, so the roadmap
    reads as an outline rather than a flat list)."""
    if not projects:
        inferred = provenance_project()
        if inferred:
            projects = [inferred]
        else:
            raise typer.BadParameter("pass --project (no session project to default from)")
    store = _store(ctx)
    items = list(store.load().items)
    if any(it.id == item_id for it in items):
        log.error(f"Roadmap item {item_id!r} already exists; use `roadmap set`.")
        raise typer.Exit(1)
    if parent and parent.strip():
        _validate_parent(items, item_id, parent.strip())
    deps = _normalize_id_list(list(depends_on)) or []
    unknown = [dep for dep in deps if not any(it.id == dep for it in items)]
    if unknown:
        log.error(f"Unknown depends-on id(s): {', '.join(unknown)}.")
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
        depends_on=tuple(deps),
        inbox_refs=tuple(_normalize_id_list(list(inbox_ref)) or ()),
        task_refs=tuple(_normalize_id_list(list(task_ref)) or ()),
        scope=ItemScope(projects=tuple(projects)),
        metadata=_apply_board_meta(
            _hierarchy_meta(
                with_provenance({"author": actor, "created_at": utc_now().isoformat()}), depth, parent
            ),
            owner=owner, milestone=milestone,
        ),
    )
    items.append(item)
    _save(store, tuple(items))
    store.append_history(item_id, _history_entry(actor, "created", after=item.status.value, note="opened"))
    if as_json:
        emit_json({**_item_dict(item), "warnings": _roadmap_warnings(items)})
        return
    log.success(f"Added roadmap item {item_id}.")
    _warn_roadmap(items)


@app.command("rename")
def rename_item(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="Current roadmap item id."),
    new_id: str = typer.Argument(..., help="New roadmap item id."),
    author: str | None = typer.Option(None, "--author", help="Who is renaming (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Rename a roadmap item id and rewrite parent/depends_on links that pointed at it."""
    new_id = new_id.strip()
    if not new_id:
        log.error("New id must be non-empty.")
        raise typer.Exit(1)
    store = _store(ctx)
    items = list(store.load().items)
    idx = next((n for n, it in enumerate(items) if it.id == item_id), None)
    if idx is None:
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    if any(it.id == new_id for it in items):
        log.error(f"Roadmap item {new_id!r} already exists.")
        raise typer.Exit(1)
    actor = author or agent_author() or items[idx].metadata.get("author")
    old = items[idx]
    items[idx] = dataclasses.replace(
        old,
        id=new_id,
        metadata={**old.metadata, "updated_at": utc_now().isoformat()},
    )
    items = _rewrite_id_refs(items, item_id, new_id)
    _save(store, tuple(items))
    store.append_history(new_id, _history_entry(actor, "renamed", before=item_id, after=new_id))
    if as_json:
        renamed = next(it for it in items if it.id == new_id)
        emit_json({**_item_dict(renamed), "renamed_from": item_id, "warnings": _roadmap_warnings(items)})
        return
    log.success(f"Renamed roadmap item {item_id} → {new_id}.")
    _warn_roadmap(items)


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
    actor = author or agent_author()
    try:
        ensure_concise_agent_message(body, "roadmap comment", author=actor)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    store.add_comment(item_id, body, actor, with_provenance())
    items = store.load().items
    if as_json:
        warnings = _roadmap_warnings(items)
        emit_json({"id": item_id, "commented": True, **({"warnings": warnings} if warnings else {})})
        return
    log.success(f"Commented on roadmap item {item_id}.")
    _warn_roadmap(items)


@app.command("remove")
def remove_item(
    ctx: typer.Context,
    item_id: str = typer.Argument(..., help="Roadmap item id to remove."),
    cascade: bool = typer.Option(
        False,
        "--cascade",
        help="Also remove direct children nested under this item (parent=item_id).",
    ),
    author: str | None = typer.Option(None, "--author", help="Who is removing (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Remove a roadmap item.

    By default, children that nested under the removed id are un-nested (parent
    cleared) rather than deleted, and ``depends_on`` entries pointing at it are
    dropped. Pass ``--cascade`` to delete direct children as well."""
    store = _store(ctx)
    loaded = list(store.load().items)
    if not any(it.id == item_id for it in loaded):
        log.error(f"No roadmap item {item_id!r}.")
        raise typer.Exit(1)
    actor = author or agent_author() or "human"
    drop = {item_id}
    if cascade:
        drop.update(it.id for it in loaded if item_parent(it) == item_id)
    kept: list[RoadmapItem] = []
    for it in loaded:
        if it.id in drop:
            continue
        meta = dict(it.metadata)
        if str(meta.get("parent") or "").strip() == item_id:
            meta.pop("parent", None)
            meta["updated_at"] = utc_now().isoformat()
        depends = tuple(dep for dep in it.depends_on if dep not in drop)
        kept.append(
            dataclasses.replace(it, depends_on=depends, metadata=meta)
            if depends != it.depends_on or meta != it.metadata
            else it
        )
    _save(store, tuple(kept))
    store.append_history(
        item_id,
        _history_entry(actor, "deleted", note="cascade" if cascade and len(drop) > 1 else "removed"),
    )
    if as_json:
        emit_json({"removed": sorted(drop), "warnings": _roadmap_warnings(kept)})
        return
    removed = ", ".join(sorted(drop))
    log.success(f"Removed roadmap item{'s' if len(drop) > 1 else ''} {removed}.")
    _warn_roadmap(kept)
