"""Assignment-scoped activity without loading request bodies or full transcripts."""

from __future__ import annotations

import json

from sqlalchemy import select, literal, union_all

from .activity_display import safe_text
from .errors import DomainError
from .schema import tables
from .usage_accounting import trusted_amount, uncertain_usage


def timeline(conn, identifier, cursor, limit, *, store=None):
    from .readmodels import page

    activity, execution, principal, api, outbox = (tables[name] for name in (
        "activity", "execution", "principal", "api_request", "outbox_operation"))
    executions = select(execution.c.id).where(execution.c.assignment_id == identifier)
    principals = select(principal.c.id).where(principal.c.execution_id.in_(executions))
    query = union_all(
        select(activity.c.id, activity.c.occurred_at, activity.c.kind, activity.c.summary,
               literal("activity").label("source"), literal(None).label("status"), activity.c.created_at.label("recorded_at"))
            .where(activity.c.assignment_id == identifier),
        select(api.c.id, api.c.created_at, literal("api"), api.c.operation, literal("api"), api.c.status, api.c.created_at)
            .where(api.c.principal_id.in_(principals)),
        select(outbox.c.id, outbox.c.created_at, outbox.c.kind, outbox.c.kind, literal("integration"), outbox.c.status, outbox.c.created_at)
            .where(outbox.c.actor_principal_id.in_(principals), outbox.c.kind.in_((
                "zulip_post", "forge_label", "forge_review", "forge_merge", "forge_create", "forge_comment", "forge_change", "forge_edit", "publication"))),
    ).subquery()
    # Replayed JSONL has a known request start but no per-item timestamp. Its
    # sequential journal delivery supplies a stable secondary order, not a fake
    # occurrence time. Include both timestamps in the pagination cursor.
    result = page(conn, select(query), query, cursor, limit, order="occurred_at", descending=True, tie_order="recorded_at")
    enrich(conn, result["items"], store=store)
    return result


def enrich(conn, rows, *, store=None):
    if not rows:
        return rows
    ids = [row["id"] for row in rows if row.get("source", "activity") == "activity"]
    usage, activity = tables["usage_record"], tables["activity"]
    originals = {row["id"]: dict(row) for row in conn.execute(select(activity).where(activity.c.id.in_(ids))).mappings()} if ids else {}
    for row in rows:
        if row["id"] in originals:
            row.update(originals[row["id"]])
    usage_rows = {row["activity_id"]: dict(row) for row in conn.execute(
        select(*[trusted_amount(usage, key).label(key) for key in
                 ("input_tokens", "cached_input_tokens", "output_tokens", "cost_usd")],
               uncertain_usage(usage).label("incomplete"),
               activity.c.id.label("activity_id"))
        .join(activity, activity.c.usage_record_id == usage.c.id).where(activity.c.id.in_(ids))).mappings()} if ids else {}
    artifact, link = tables["artifact"], tables["activity_artifact"]
    displays = {}
    if store and ids:
        for row in conn.execute(select(link.c.activity_id, artifact.c.content).join(artifact)
                               .where(link.c.activity_id.in_(ids), artifact.c.kind == "blob")
                               .order_by(artifact.c.created_at)).mappings():
            content = row["content"]
            size = content.get("size_bytes")
            if not isinstance(size, int) or size > 131072 or not content.get("sha256") or row["activity_id"] in displays:
                continue
            try:
                data = json.loads(store.read(content["sha256"], size))
                display = data.get("horizon_activity") if isinstance(data, dict) else None
                if isinstance(display, dict):
                    displays[row["activity_id"]] = display
            except (OSError, ValueError, KeyError, DomainError):
                continue
    for row in rows:
        source = row.get("source", "activity")
        display = displays.get(row["id"], {})
        measured = usage_rows.get(row["id"])
        category = display.get("category") or ("usage" if measured else (
            "api" if source == "api" else "forge" if row["kind"].startswith("forge_") else
            "zulip" if row["kind"] == "zulip_post" else "tool" if row["kind"] == "tool_use" else "lifecycle"))
        title = display.get("title") or row.get("summary")
        if measured:
            title = "Provider usage: " + ", ".join(f"{measured[key]:,} {label}" for key, label in (
                ("input_tokens", "input tokens"), ("output_tokens", "output tokens")) if measured.get(key) is not None)
            measured.pop("activity_id", None)
        elif source in ("api", "integration"):
            title = f"{str(title).replace('_', ' ')}: {row['status']}"
        row.update(source=source, category=category, title=safe_text(title or row["kind"].replace("_", " "), 300),
                   detail=safe_text(display.get("detail")) or None, usage=measured)
    return rows
