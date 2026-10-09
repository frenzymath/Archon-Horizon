"""Byte-bounded current work state; histories remain behind explicit links."""

import json

from sqlalchemy import ARRAY, JSON, String, Text, case, cast, func, select

from ..persistence.records import json_value
from ..persistence.schema import tables

MAX_BYTES = 32768


def size(value):
    return len(json.dumps(json_value(value), ensure_ascii=False, separators=(",", ":")).encode())


def record(row, kind, fields, *, text_limit=768):
    result = {key: row[key] for key in fields if key in row}
    result["detail_url"] = f"/api/v3/records/{kind}/{row['id']}"
    truncated = []
    for key, value in list(result.items()):
        if key == "detail_url":
            continue
        if row.get("_omitted_" + key):
            del result[key]
            truncated.append(key)
        elif isinstance(value, str) and len(value.encode()) > text_limit:
            result[key] = value.encode()[:text_limit].decode(errors="ignore")
            truncated.append(key)
        elif isinstance(value, (list, dict)) and size(value) > 4096:
            del result[key]
            truncated.append(key)
    if truncated:
        result["truncated_fields"] = truncated
    return result


def columns(table, fields, *, text_limit=768):
    """Do not transfer full reports/transcripts merely to truncate them in Python."""
    result = []
    for field in fields:
        column = table.c[field]
        if isinstance(column.type, String):
            result.append(func.left(column, text_limit + 1).label(field))
        elif isinstance(column.type, (JSON, ARRAY)):
            oversized = func.octet_length(cast(column, Text)) > 4096
            result.extend([case((oversized, None), else_=column).label(field), oversized.label("_omitted_" + field)])
        else:
            result.append(column)
    return result


def build(conn, service, assignment, mission, run, control, coordination):
    aid = assignment["id"]
    result = {
        "view": "brief", "schema_version": 1,
        "assignment": record(assignment, "assignment", (
            "id", "number", "revision", "run_id", "mission_id", "role", "functions",
            "status", "status_note", "instructions", "not_before", "expires_at", "start_condition",
            "automation_id", "workspace_id", "checkpoint_requested_at")),
        "mission": record(mission, "mission", (
            "id", "number", "revision", "project_id", "title", "objective", "status", "parent_id",
            "roadmap_document_id", "acceptance_criteria", "delegation_note", "scope",
            "max_open_children"), text_limit=4096),
        "run": record(run, "run", (
            "id", "number", "revision", "status", "phase", "max_assignments", "token_budget", "expires_at",
            "adopted_roadmap_snapshot_id")),
        "control_notices": control, "coordination": coordination,
        "collections": {}, "current_provider_request": None,
        "detail_url": f"/api/v3/assignments/{aid}/context?view=full",
        "context_note": "Current state only. Read detail_url for truncated fields before acting on them. "
                        "Collection links retain complete history; receiving a notice does not resolve it.",
    }
    thread, request = tables["provider_thread"], tables["provider_request"]
    current = conn.execute(select(request).join(thread, request.c.provider_thread_id == thread.c.id).where(
        thread.c.assignment_id == aid, thread.c.kind == "primary",
        request.c.status.in_(("pending", "submitted", "running"))).order_by(request.c.created_at.desc()).limit(1)).mappings().first()
    if current:
        result["current_provider_request"] = record(current, "provider_request", (
            "id", "number", "status", "reason", "created_at", "provider_thread_id", "execution_id"))
    fields = {
        "obligation": ("id", "number", "revision", "kind", "description", "status", "updated_at"),
        "notification": ("id", "revision", "event_id", "urgency", "disposition", "created_at"),
        "execution": ("id", "revision", "status", "host_id", "started_at", "failure"),
        "provider_thread": ("id", "revision", "kind", "status", "provider_thread_id", "description"),
        "activity": ("id", "kind", "occurred_at"),
    }
    for kind, maximum in (("obligation", 12), ("notification", 8), ("execution", 2),
                          ("provider_thread", 1), ("activity", 0)):
        table = tables[kind]
        query = select(*columns(table, fields[kind])).where(table.c.assignment_id == aid)
        total = conn.execute(select(func.count()).select_from(query.subquery())).scalar_one()
        if kind == "obligation":
            query = query.where(table.c.status == "open").order_by(table.c.number)
        elif kind == "notification":
            query = query.where(table.c.disposition == "pending").order_by(
                (table.c.urgency == "control").desc(), (table.c.urgency == "direct").desc())
        elif kind == "execution":
            query = query.where(table.c.status.in_(("starting", "running", "stopping")))
        elif kind == "provider_thread":
            query = query.where(table.c.kind == "primary")
        selected = (total if kind == "activity" else
            conn.execute(select(func.count()).select_from(query.order_by(None).subquery())).scalar_one())
        key = "activity" if kind == "activity" else kind + "s"
        result[key] = []
        result["collections"][key] = {
            "total": total, "selected": selected, "included": 0, "truncated": total > 0,
            "list_url": f"/api/v3/records/{kind}?assignment_id={aid}",
        }
        for row in conn.execute(query.order_by(table.c.created_at.desc()).limit(maximum)).mappings():
            result[key].append(record(row, kind, fields[kind]))
    # Reserve space for the manifest and never emit a half-valid nested condition.
    for key in ("activity", "provider_threads", "executions", "notifications", "obligations"):
        while result[key] and size(result) > MAX_BYTES - 512:
            result[key].pop()
    if size(result) > MAX_BYTES - 512:
        global_view = coordination.get("global", {})
        compact_global = {
            "status": global_view.get("status"),
            "capacity": global_view.get("capacity", {}),
            "pending": {key: global_view.get("pending", {}).get(key)
                        for key in ("total", "oldest_age_seconds")},
            "active": {"by_role": global_view.get("active", {}).get("by_role", {}),
                       "live_by_role": global_view.get("active", {}).get("live_by_role", {})},
            "maintainer_admission": global_view.get("maintainer_admission", {}),
        }
        result["coordination"] = {"detail_url": result["detail_url"], "truncated": True,
            "global": compact_global, "memory": coordination.get("memory", {})}
    if size(result) > MAX_BYTES - 512:
        result["coordination"] = {"detail_url": result["detail_url"], "truncated": True,
            "memory": {"episode": coordination.get("memory", {}).get("episode")}}
    if size(result) > MAX_BYTES - 512:
        notices = result["control_notices"]
        notices["omitted"] = notices["pending"]
        notices["notices"] = []
    for key, collection in result["collections"].items():
        collection["included"] = len(result[key])
        collection["truncated"] = collection["total"] > len(result[key])
    result["byte_budget"] = MAX_BYTES
    # Escaped text can be larger than its source UTF-8 bytes. Preserve typed
    # identifiers and exact detail links even for unusually verbose metadata.
    if size(result) > MAX_BYTES:
        for key, fields_to_keep in (("mission", ("id", "number", "revision", "project_id", "status")),
                                    ("assignment", ("id", "number", "revision", "run_id", "mission_id", "status")),
                                    ("run", ("id", "number", "revision", "status"))):
            if size(result) <= MAX_BYTES:
                break
            value = result[key]
            omitted = [field for field in value if field not in (*fields_to_keep, "detail_url", "truncated_fields")]
            result[key] = {field: value[field] for field in (*fields_to_keep, "detail_url") if field in value}
            result[key]["truncated_fields"] = list(dict.fromkeys([*value.get("truncated_fields", []), *omitted]))
    return result
