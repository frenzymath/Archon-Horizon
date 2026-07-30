"""Typer command group for the local inbox."""

from __future__ import annotations

import os

import typer

from archon_horizon.core.collection_health import inbox_health_warnings
from archon_horizon.core.inbox import (
    PARTICIPANTS_KEY,
    READ_BY_KEY,
    STARTED_BY_KEY,
    InboxDraft,
    InboxFilter,
    InboxKind,
    InboxScope,
    InboxStatus,
    audience_targets,
    inbox_attention,
    inbox_attention_rank,
    is_conversation,
    is_read_by,
    matches_filter,
    reaches_horizon,
)
from archon_horizon.core.labels import AGENT_READY, NOT_READY, REJECTED, is_agent_ready
from archon_horizon.log import log
from archon_horizon.store import serde

from .shared import agent_author as _agent_author
from .shared import (
    conversation_sender,
    emit_json,
    ensure_concise_agent_message,
    load_workspace,
    local_inbox,
    provenance_task,
    provenance_project,
    reader_id,
    task_inbox_refs,
    with_provenance,
)

app = typer.Typer(help="Manage the local inbox.", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _clean_agent(detail: str | None, role: str) -> str | None:
    """Tidy a sub-identity: drop a redundant leading ``<role>-`` / ``<role> ``
    prefix (so ``horizon-work-reviewer`` under author ``horizon`` reads ``work-reviewer``)."""
    if not detail:
        return None
    text = detail.strip()
    if role:
        for prefix in (f"{role}-", f"{role} ", f"{role}/", f"{role}:"):
            if text.lower().startswith(prefix):
                text = text[len(prefix):].strip()
                break
    return text or None


def _resolve_authorship(author: str | None, agent: str | None = None) -> tuple[str, str | None]:
    """Canonical author plus an optional finer-grained sub-identity.

    Inbox authors are a small conventional set (human / horizon / …) so
    the UI can group and colour by them. Agents run with
    ``ARCHON_HORIZON_AGENT_ROLE`` set to their role, and that role is authoritative
    — it wins over any ``--author`` the model typed. A more specific identity (a
    subagent descriptor name like ``blueprint-reviewer``, passed via ``--agent`` or
    typed into ``--author``) is preserved separately as the ``agent`` detail rather
    than leaking into the author field. A direct human CLI caller (no agent role in
    the environment) keeps whatever ``--author`` they pass."""
    detail = (agent or "").strip() or None
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    role = role if role == "horizon" else ""
    if role:
        # A subagent that typed its own name into --author: demote it to detail.
        if detail is None and author and author.strip().lower() != role:
            detail = author.strip()
        return role, _clean_agent(detail, role)
    canonical = author.strip() if author and author.strip() else "human"
    return canonical, _clean_agent(detail, canonical.lower())


def _inbox(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return local_inbox(workspace)


def _warn_inbox(items) -> list[str]:
    warnings = inbox_health_warnings(items)
    for warning in warnings:
        log.warn(warning)
    return warnings


def _with_inbox_warnings(payload: dict, items) -> dict:
    """Keep legacy JSON shapes when healthy; attach actionable warnings only."""
    warnings = inbox_health_warnings(items)
    return {**payload, **({"warnings": warnings} if warnings else {})}


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


def _normalize_recipients(recipients: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    targets = audience_targets(",".join(recipients))
    allowed = ("task:", "run:", "project:")
    invalid = [
        target for target in targets
        if target not in {"horizon", "human"} and not target.startswith(allowed)
    ]
    if invalid:
        raise typer.BadParameter(
            "unknown recipient(s): " + ", ".join(invalid)
            + "; use human, horizon, task:<id>, run:<id>, or project:<name>"
        )
    return targets


def _item_dict(item) -> dict:
    return {
        "id": item.id,
        "provider": item.provider,
        "kind": item.kind.value,
        "attention": inbox_attention(item).value,
        "conversation": is_conversation(item),
        "status": item.status.value,
        "labels": list(item.labels),
        "body": item.body,
        "scope": serde.to_jsonable(item.scope),
        "audience": getattr(item, "audience", "") or "",
        "author": getattr(item, "author", "") or "",
        "owner_task": str(item.metadata.get("owner_task", "") or ""),
        "read_by": list(item.metadata.get("read_by", []) or []),
        "source_ref": item.source_ref,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "comments": list(item.metadata.get("comments", [])),
        "metadata": {k: v for k, v in item.metadata.items() if k != "comments"},
    }


def _latest_comments(item, cap: int | None) -> list[dict]:
    comments = list(item.metadata.get("comments", []))
    if cap is None:
        return comments
    if cap <= 0:
        return []
    return comments[-cap:]


def _item_dict_capped(item, comments: int | None = None) -> dict:
    data = _item_dict(item)
    data["comments"] = _latest_comments(item, comments)
    return data


def _item_title(item, limit: int = 72) -> str:
    title = str(item.body or "").split("\n", 1)[0].strip()
    return title if len(title) <= limit else title[:limit - 3].rstrip() + "..."


def _attention_summary(items, reader: str) -> dict:
    """Compact, front-loaded triage that survives stderr redirection in JSON mode."""
    open_items = [item for item in items if item.status is InboxStatus.OPEN]
    protections = sorted(
        (item for item in open_items if item.kind is InboxKind.PROTECTION),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    conversations = sorted(
        (
            item for item in open_items
            if is_conversation(item) and not is_read_by(item, reader)
        ),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    advisory_unread = sum(
        not is_conversation(item)
        and item.kind is not InboxKind.PROTECTION
        and not is_read_by(item, reader)
        for item in open_items
    )
    def row(item) -> dict:
        return {
            "id": item.id,
            "title": _item_title(item),
            "author": item.author,
            "audience": item.audience,
            "updated_at": item.updated_at.isoformat(),
        }
    return {
        "required_protections": [row(item) for item in protections],
        "unread_conversations": [row(item) for item in conversations],
        "advisory_unread_count": advisory_unread,
        "order": ["required_protections", "unread_conversations", "advisory"],
    }


@app.command("list")
def list_items(
    ctx: typer.Context,
    status: InboxStatus | None = typer.Option(None, "--status", help="Only show this status: open, closed, or archived."),
    kind: list[InboxKind] = typer.Option((), "--kind", help="Only show this kind. Repeat for several kinds."),
    label: list[str] = typer.Option((), "--label", help="Require this label. Repeat to require several labels."),
    project: str | None = typer.Option(None, "--project", help="Only show items scoped to this project."),
    to: str | None = typer.Option(None, "--to", help="Only show items addressed to this audience; use '' for general items."),
    task: str | None = typer.Option(None, "--task", help="A task's inbox: items owned by this task PLUS shared (unowned) items."),
    mine: bool = typer.Option(False, "--mine", help="Shortcut for --task <my task>, from the session environment."),
    unread: bool = typer.Option(False, "--unread", help="Only items you (this task/run/human) have not marked read."),
    query: str = typer.Option("", "--query", "-q", help="Case-insensitive search across ids, body, labels, audience, author, and scope."),
    limit: int | None = typer.Option(None, "--limit", "-n", min=1, help="Show only the N most recently updated matching items."),
    comments: int | None = typer.Option(None, "--comments", min=0, help="Include only the N latest comments per item; 0 hides comments."),
    as_json: bool = _JSON,
) -> None:
    """List local inbox items, optionally narrowed for triage."""
    in_session = bool(
        os.environ.get("ARCHON_HORIZON_SESSION", "").strip()
        or os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip()
    )
    if mine and task is None:
        task = provenance_task()
    if in_session and task is None and to is None:
        # An agent's unqualified inbox is its team inbox, not a see-all audit of
        # other teams' owned items.
        task = provenance_task()
    if in_session and status is None:
        # Agents need a working queue, not hundreds of archived historical
        # records. An explicit --status still permits an audit.
        status = InboxStatus.OPEN
    filters = InboxFilter(
        status=status,
        kinds=tuple(kind),
        labels=tuple(label),
        project=project,
        audience=to if to is not None else None,
        owner_task=task,
        unread_for=reader_id() if unread else None,
        query=query,
    )
    _, workspace = load_workspace(ctx.obj["root"])
    inbox = local_inbox(workspace)
    all_items = inbox.list_items()
    project_context = provenance_project()
    task_context = provenance_task()
    run_context = os.environ.get("ARCHON_HORIZON_RUN", "").strip() or None
    inbox_refs = task_inbox_refs(workspace, task_context)
    visible_items = [
        item for item in all_items
        if not in_session or (
            is_agent_ready(item.labels)
            and reaches_horizon(
                item, project_context, task=task_context, run=run_context,
                inbox_refs=inbox_refs,
            )
        )
    ]
    warnings = inbox_health_warnings(visible_items)
    items = sorted(
        (item for item in visible_items if matches_filter(item, filters)),
        key=lambda item: (inbox_attention_rank(item), item.updated_at),
        reverse=True,
    )
    if limit is not None:
        items = items[:limit]
    if as_json:
        emit_json({
            "attention": _attention_summary(visible_items, reader_id()),
            "items": [_item_dict_capped(i, comments) for i in items],
            "warnings": warnings,
        })
        return
    if not items:
        log.info("(empty)")
        _warn_inbox(visible_items)
        return
    me = reader_id()
    rows = []
    for item in items:
        labels = ",".join(item.labels) or "unlabeled"
        audience_text = f" to:{item.audience}" if item.audience else ""
        owner = str(item.metadata.get("owner_task", "") or "")
        owner_text = f" owner:{owner}" if owner else ""
        unread_mark = "" if is_read_by(item, me) else "● "
        scope_projects = item.scope.targets("projects")
        scope_text = f" project:{','.join(scope_projects)}" if scope_projects else ""
        summary = f"{unread_mark}{item.kind.value} [{labels}]{audience_text}{owner_text}{scope_text} {item.body[:80]}"
        capped_comments = _latest_comments(item, comments)
        if capped_comments:
            comment_bits = []
            for comment in capped_comments:
                author = str(comment.get("author") or "local")
                body = str(comment.get("body") or "").replace("\n", " ")[:90]
                comment_bits.append(f"{author}: {body}")
            summary = f"{summary}\n  comments: " + " | ".join(comment_bits)
        rows.append((item.id, item.status.value, summary))
    log.results_table(rows, title="Local Inbox")
    _warn_inbox(visible_items)


@app.command()
def add(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="Inbox item body."),
    kind: InboxKind = typer.Option(InboxKind.HINT, "--kind", help="Item kind."),
    project: str | None = typer.Option(None, "--project", help="Scope the item to a project (what it is ABOUT)."),
    to: str | None = typer.Option(
        None, "--to",
        help="Recipient the item is FOR: horizon | human | project:<name> | task:<id> | run:<id>. "
             "task:/run: is a direct message that reaches only that recipient.",
    ),
    owner: str | None = typer.Option(
        None, "--owner",
        help="Own the item to a task's inbox (its id). Omit for shared/everyone.",
    ),
    mine: bool = typer.Option(False, "--mine", help="Own the item to my task, from the session environment."),
    author: str | None = typer.Option(None, "--author", help="Who is writing it (human / horizon)."),
    agent: str | None = typer.Option(
        None, "--agent",
        help="Finer-grained author identity (e.g. a subagent descriptor name like "
             "'blueprint-reviewer'); recorded in metadata so the author stays the "
             "conventional role.",
    ),
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
    author_val, agent_val = _resolve_authorship(author, agent)
    _, _, description = body.partition("\n\n")
    if not description:
        _, _, description = body.partition("\n")
    if author_val == "horizon":
        try:
            ensure_concise_agent_message(
                description, "description", author=author_val,
                soft_limit=800, hard_limit=2400,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    metadata = with_provenance()
    recipients = _normalize_recipients([to]) if to else ()
    conversation = len(recipients) > 1 or any(
        target.startswith(("task:", "run:")) for target in recipients
    )
    if conversation:
        sender = conversation_sender()
        metadata.update({
            "conversation": True,
            PARTICIPANTS_KEY: list(dict.fromkeys((*recipients, sender))),
            STARTED_BY_KEY: sender,
            READ_BY_KEY: [reader_id()],
        })
    if agent_val:
        metadata["agent"] = agent_val
    if mine and owner is None:
        owner = provenance_task()
    if owner:
        metadata["owner_task"] = owner
    inbox = _inbox(ctx)
    created = inbox.create_item(
        InboxDraft(kind=InboxKind.CONVERSATION if conversation else kind,
                   body=body, labels=labels, scope=scope,
                   audience=", ".join(recipients),
                   author=author_val, metadata=metadata)
    )
    if as_json:
        emit_json(_with_inbox_warnings(_item_dict(created), inbox.list_items()))
        return
    log.success(f"created {created.id}")
    _warn_inbox(inbox.list_items())


@app.command()
def dm(
    ctx: typer.Context,
    recipient: list[str] = typer.Argument(
        ..., help="One or more recipients: task:<id>, run:<id>, project:<name>, human, or horizon."
    ),
    body: str = typer.Option(..., "--body", help="Conversation title, blank line, then opening message."),
    project: str | None = typer.Option(None, "--project", help="Project the conversation is about."),
    author: str | None = typer.Option(None, "--author", help="Who is starting it (normally inferred)."),
    agent: str | None = typer.Option(None, "--agent", help="Finer-grained agent identity."),
    as_json: bool = _JSON,
) -> None:
    """Start a direct or group conversation in the inbox.

    The opening message becomes a durable thread; humans reply in the dashboard
    and agents reply with ``horizon inbox comment <id>``. The originating task/run
    remains a participant even when it is not listed as a recipient.
    """
    _ensure_title_and_description(body)
    recipients = _normalize_recipients(recipient)
    if not recipients:
        raise typer.BadParameter("provide at least one recipient")
    author_val, agent_val = _resolve_authorship(author, agent)
    _, _, description = body.partition("\n\n")
    if not description:
        _, _, description = body.partition("\n")
    if author_val == "horizon":
        try:
            ensure_concise_agent_message(
                description, "opening message", author=author_val,
                soft_limit=800, hard_limit=2400,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    sender = conversation_sender()
    metadata = with_provenance({
        "conversation": True,
        PARTICIPANTS_KEY: list(dict.fromkeys((*recipients, sender))),
        STARTED_BY_KEY: sender,
        READ_BY_KEY: [reader_id()],
    })
    if agent_val:
        metadata["agent"] = agent_val
    inbox = _inbox(ctx)
    created = inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body=body,
        labels=(AGENT_READY,),
        scope=InboxScope(projects=(project,) if project else ()),
        audience=", ".join(recipients),
        author=author_val,
        metadata=metadata,
    ))
    if as_json:
        emit_json(_with_inbox_warnings(_item_dict(created), inbox.list_items()))
        return
    log.success(f"started conversation {created.id} with {', '.join(recipients)}")
    _warn_inbox(inbox.list_items())


@app.command()
def show(
    ctx: typer.Context,
    id: str,
    mark_read: bool = typer.Option(
        True, "--mark-read/--no-mark-read",
        help="Mark this thread read for the current task/run/human.",
    ),
    as_json: bool = _JSON,
) -> None:
    """Open one inbox item directly; by default, acknowledge it as read."""
    _, workspace = load_workspace(ctx.obj["root"])
    inbox = local_inbox(workspace)
    item = inbox.get_item(id)
    in_session = bool(
        os.environ.get("ARCHON_HORIZON_SESSION", "").strip()
        or os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip()
    )
    task_context = provenance_task()
    run_context = os.environ.get("ARCHON_HORIZON_RUN", "").strip() or None
    if in_session and (
        not is_agent_ready(item.labels)
        or not reaches_horizon(
            item, provenance_project(), task=task_context, run=run_context,
            inbox_refs=task_inbox_refs(workspace, task_context),
        )
    ):
        raise typer.BadParameter(f"{id} is not addressed to this team")
    if mark_read:
        inbox.set_read(id, reader_id(), read=True, actor=_agent_author())
        item = inbox.get_item(id)
    payload = _item_dict(item)
    if as_json:
        emit_json(payload)
        return
    log.key_value({
        "id": item.id,
        "kind": item.kind.value,
        "attention": inbox_attention(item).value,
        "status": item.status.value,
        "from": item.author or "unknown",
        "to": item.audience or "everyone",
        "updated": item.updated_at.isoformat(),
    }, title="Inbox item")
    log.panel(item.body, title=_item_title(item))
    for comment in item.metadata.get("comments", []):
        if not isinstance(comment, dict):
            continue
        author = str(comment.get("author") or "local")
        at = str(comment.get("at") or "")
        log.panel(str(comment.get("body") or ""), title=f"{author} {at}".strip())


@app.command(hidden=True)
def comment(
    ctx: typer.Context,
    id: str,
    body: str = typer.Option(..., "--body", help="Comment body."),
    author: str | None = typer.Option(None, "--author", help="Comment author."),
    as_json: bool = _JSON,
) -> None:
    """Add a progress comment to an inbox item (track work, not ask the human)."""
    author_val, _ = _resolve_authorship(author)
    if author_val == "horizon":
        try:
            ensure_concise_agent_message(body, "comment", author=author_val)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    inbox = _inbox(ctx)
    inbox.add_comment(id, body, author=author_val, metadata=with_provenance())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "commented": True}, items))
        return
    log.success(f"commented on {id}")
    _warn_inbox(items)


@app.command()
def protect(
    ctx: typer.Context,
    body: str = typer.Option(..., "--body", help="The constraint, e.g. 'do not change the signature of Foo.bar'."),
    project: str | None = typer.Option(None, "--project", help="Restrict to a project."),
    file: str | None = typer.Option(None, "--file", help="Restrict to a file."),
    declaration: str | None = typer.Option(None, "--declaration", help="Restrict to a declaration."),
    blueprint_node: str | None = typer.Option(
        None, "--blueprint-node", help="Restrict to a blueprint node label or id."
    ),
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
        blueprint_nodes=(blueprint_node,) if blueprint_node else (),
    )
    inbox = _inbox(ctx)
    created = inbox.create_item(
        InboxDraft(
            kind=InboxKind.PROTECTION,
            body=f"[persistent] {body}",
            labels=(AGENT_READY,),
            scope=scope,
            audience="horizon",
            author=_resolve_authorship(None)[0],
            metadata=with_provenance(),
        )
    )
    if as_json:
        emit_json(_with_inbox_warnings(_item_dict(created), inbox.list_items()))
        return
    log.success(f"protected {created.id}")
    _warn_inbox(inbox.list_items())


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
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "edited": True}, items))
        return
    log.success(f"edited {id}")
    _warn_inbox(items)


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
    inbox = _inbox(ctx)
    inbox.update_comment(id, index, body, author=author)
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "index": index, "edited": True}, items))
        return
    log.success(f"edited comment {index} on {id}")
    _warn_inbox(items)


@app.command(hidden=True)
def label(
    ctx: typer.Context,
    id: str,
    labels: list[str] = typer.Argument(...),
    as_json: bool = _JSON,
) -> None:
    """Replace labels on an inbox item."""
    inbox = _inbox(ctx)
    inbox.update_labels(id, labels, _agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "labels": list(labels)}, items))
        return
    log.success(f"relabeled {id}")
    _warn_inbox(items)


@app.command()
def complete(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Mark an inbox item completed."""
    inbox = _inbox(ctx)
    inbox.update_status(id, InboxStatus.CLOSED, _agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "status": InboxStatus.CLOSED.value}, items))
        return
    log.success(f"completed {id}")
    _warn_inbox(items)


@app.command()
def archive(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Archive an inbox item: a soft-delete that keeps the record but hides it
    from the dashboard by default (the human can opt to show archived items)."""
    inbox = _inbox(ctx)
    inbox.update_status(id, InboxStatus.ARCHIVED, _agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "status": InboxStatus.ARCHIVED.value}, items))
        return
    log.success(f"archived {id}")
    _warn_inbox(items)


@app.command(hidden=True)
def reject(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Mark an inbox item rejected."""
    inbox = _inbox(ctx)
    inbox.update_labels(id, [REJECTED], _agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "rejected": True}, items))
        return
    log.success(f"rejected {id}")
    _warn_inbox(items)


@app.command()
def read(
    ctx: typer.Context,
    id: str,
    reader: str | None = typer.Option(None, "--reader", help="Reader id (defaults to my task / run / human)."),
    as_json: bool = _JSON,
) -> None:
    """Mark an inbox item read by you (a task, run, or human).

    Read-state is per-reader: a shared item several teams see tracks who has read
    it, so each team can find what is still unread for them (`inbox list --unread`).
    """
    who = (reader or reader_id())
    inbox = _inbox(ctx)
    inbox.set_read(id, who, read=True, actor=_agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "read": True, "reader": who}, items))
        return
    log.success(f"marked {id} read by {who}")
    _warn_inbox(items)


@app.command()
def unread(
    ctx: typer.Context,
    id: str,
    reader: str | None = typer.Option(None, "--reader", help="Reader id (defaults to my task / run / human)."),
    as_json: bool = _JSON,
) -> None:
    """Mark an item unread again — e.g. you read it but it is still relevant/unactioned."""
    who = (reader or reader_id())
    inbox = _inbox(ctx)
    inbox.set_read(id, who, read=False, actor=_agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "read": False, "reader": who}, items))
        return
    log.success(f"marked {id} unread for {who}")
    _warn_inbox(items)


@app.command()
def own(
    ctx: typer.Context,
    id: str,
    owner: str | None = typer.Option(None, "--owner", help="Owning task id; omit (or --shared) for everyone."),
    mine: bool = typer.Option(False, "--mine", help="Own to my task, from the session environment."),
    shared: bool = typer.Option(False, "--shared", help="Share with everyone (clear ownership)."),
    as_json: bool = _JSON,
) -> None:
    """Move an item into a task's inbox, or share it with everyone."""
    target = "" if shared else (owner or (provenance_task() if mine else None))
    if target is None:
        raise typer.BadParameter("pass --owner <task>, --mine, or --shared")
    inbox = _inbox(ctx)
    inbox.set_owner(id, target, actor=_agent_author())
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "owner_task": target}, items))
        return
    log.success(f"{id} now owned by {target or 'everyone'}")
    _warn_inbox(items)


@app.command()
def delete(ctx: typer.Context, id: str, as_json: bool = _JSON) -> None:
    """Delete an inbox item."""
    inbox = _inbox(ctx)
    inbox.delete_item(id)
    items = inbox.list_items()
    if as_json:
        emit_json(_with_inbox_warnings({"id": id, "deleted": True}, items))
        return
    log.success(f"deleted {id}")
    _warn_inbox(items)
