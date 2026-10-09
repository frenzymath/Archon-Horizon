"""Small relational primitives shared by transactional domain services."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import time
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from ..errors import DomainError
from .schema import tables

REVISIONED = {"project", "mission", "node", "document", "reference", "assignment",
              "automation", "harness", "review_policy", "reviewer_descriptor"}
SCOPE_PARENT = {
    "run": ("mission", "mission_id"), "assignment": ("run", "run_id"),
    "automation": ("run", "run_id"), "execution": ("assignment", "assignment_id"),
    "provider_thread": ("assignment", "assignment_id"), "provider_request": ("execution", "execution_id"),
    "obligation": ("assignment", "assignment_id"), "activity": ("assignment", "assignment_id"),
    "publication": ("artifact", "artifact_id"), "forge_item": ("repository", "repository_id"),
    "message": ("discussion", "discussion_id"), "review_gate": ("forge_item", "forge_item_id"),
    "forge_review": ("forge_item", "forge_item_id"), "verification": ("artifact", "artifact_id"),
    "subscription": ("assignment", "assignment_id"), "notification": ("assignment", "assignment_id"),
}


def json_value(value):
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def canonical(value) -> bytes:
    return json.dumps(json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def transaction_lock(conn, *, wait: bool = True) -> bool:
    # Acquire before row locks. This short mutation lock also makes event IDs commit ordered.
    # No external I/O or provider execution may run in these transactions.
    started = time.monotonic()
    if wait:
        conn.execute(text("SELECT pg_advisory_xact_lock(6847901423490021)"))
        acquired = True
    else:
        acquired = conn.execute(text("SELECT pg_try_advisory_xact_lock(6847901423490021)")).scalar_one()
    elapsed = time.monotonic() - started
    if elapsed >= 1:
        logging.getLogger(__name__).warning("Mutation lock wait %.3fs", elapsed)
    if acquired:
        conn.info.setdefault("horizon_mutation_lock_started", time.monotonic())
    return acquired


def get(conn, kind: str, identifier: UUID | str, *, lock: bool = False) -> dict:
    table = tables[kind]
    try:
        identifier = UUID(str(identifier))
    except (ValueError, TypeError, AttributeError):
        raise DomainError("invalid_identifier", "Record identifier must be a UUID", 422) from None
    query = select(table).where(table.c.id == identifier)
    if lock:
        query = query.with_for_update()
    row = conn.execute(query).mappings().first()
    if row is None:
        raise DomainError("not_found", f"{kind.replace('_', ' ').capitalize()} was not found", 404)
    return dict(row)


def create(conn, table_name: str, **values) -> dict:
    table = tables[table_name]
    return dict(conn.execute(table.insert().values(**values).returning(table)).mappings().one())


def change(conn, table_name: str, identifier, expected_revision: int | None = None, **values) -> dict:
    table = tables[table_name]
    where = [table.c.id == identifier]
    if expected_revision is not None:
        where.append(table.c.revision == expected_revision)
    row = conn.execute(update(table).where(*where).values(
        **values, updated_at=func.now(), revision=table.c.revision + 1,
    ).returning(table)).mappings().first()
    if row is None:
        current = get(conn, table_name, identifier)
        raise DomainError("revision_conflict", "The record changed; refresh and retry",
                          current_revision=current["revision"])
    return dict(row)


def next_number(conn, kind: str, scope_column: str, scope_id: UUID) -> int:
    table = tables[kind]
    return conn.execute(select(func.coalesce(func.max(table.c.number), 0) + 1).where(
        table.c[scope_column] == scope_id)).scalar_one()


def project_of(conn, kind: str, identifier: UUID | str) -> UUID | None:
    row = get(conn, kind, identifier)
    if kind == "project":
        return row["id"]
    if "project_id" in row:
        return row["project_id"]
    if kind in SCOPE_PARENT:
        parent, key = SCOPE_PARENT[kind]
        return project_of(conn, parent, row[key])
    return None


def scoped_query(kind: str, project_id: UUID):
    table = tables[kind]
    current, joined = table, table
    while "project_id" not in current.c:
        if current.name == "project":
            return select(table).select_from(joined).where(current.c.id == project_id)
        if current.name not in SCOPE_PARENT:
            raise DomainError("installation_scope", "This record belongs to the installation", 422)
        parent, key = SCOPE_PARENT[current.name]
        target = tables[parent]
        joined = joined.join(target, current.c[key] == target.c.id)
        current = target
    return select(table).select_from(joined).where(current.c.project_id == project_id)


def same_project(conn, kind: str, identifier, project_id: UUID) -> dict:
    if project_of(conn, kind, identifier) != project_id:
        raise DomainError("scope_mismatch", "Referenced records must belong to the same project", 422)
    return get(conn, kind, identifier)


def object_ref(conn, kind: str, identifier, path: str | None = None) -> UUID:
    table = tables["object_reference"]
    key = "repository_id" if kind in ("file", "directory") else f"{kind}_id"
    values = {"kind": kind, key: UUID(str(identifier))}
    if path is not None:
        values["path"] = path
    result = conn.execute(insert(table).values(**values).on_conflict_do_nothing().returning(table.c.id)).scalar_one_or_none()
    if result is None:
        query = select(table.c.id).where(table.c.kind == kind, table.c[key] == values[key])
        if path is not None:
            query = query.where(table.c.path == path)
        result = conn.execute(query).scalar_one()
    return result


def snapshot(conn, kind: str, row: dict, actor_id: UUID) -> UUID:
    if kind not in REVISIONED:
        raise ValueError(f"{kind} does not use business revisions")
    table = tables["record_revision"]
    ref = object_ref(conn, kind, row["id"])
    current = conn.execute(select(table.c.id).where(
        table.c.object_id == ref, table.c.object_revision == row["revision"],
    )).scalar_one_or_none()
    if current:
        return current
    from ..models import RELATIONS
    content = dict(row)
    for field, (join_name, own_key, other_key) in RELATIONS.get(kind, {}).items():
        relation = tables[join_name]
        content[field] = list(conn.execute(select(relation.c[other_key]).where(
            relation.c[own_key] == row["id"]).order_by(relation.c[other_key])).scalars())
    return create(conn, "record_revision", object_id=ref, object_revision=row["revision"],
                  schema_version=1, actor_principal_id=actor_id, content=json_value(content))["id"]


def emit(conn, actor_id: UUID | None, project_id: UUID | None, kind: str | None, row: dict,
         changed_fields: list[str], *, previous: str | None = None,
         execution_id: UUID | None = None, source: str = "control_plane",
         source_event_id: str | None = None, note: str | None = None) -> dict:
    payload = {"revision": row.get("revision", 1), "changed_fields": changed_fields}
    if note is not None:
        payload["note"] = note
    event_kind = "record_changed"
    if previous is not None:
        event_kind = "status_changed"
        payload = {"revision": row["revision"], "previous": previous, "current": row["status"],
                   "note": note if note is not None else row.get("status_note", row.get("closure_note"))}
    return create(conn, "event", project_id=project_id,
                  subject_id=object_ref(conn, kind, row["id"]) if kind else None, actor_principal_id=actor_id,
                  execution_id=execution_id, kind=event_kind, schema_version=1, source=source,
                  source_event_id=source_event_id or str(uuid4()), occurred_at=datetime.now(timezone.utc),
                  payload=payload)


def save_blob(conn, store, project_id: UUID, value, execution_id: UUID | None = None) -> dict:
    content = store.put_json(json_value(value))
    table = tables["artifact"]
    existing = conn.execute(select(table).where(
        table.c.project_id == project_id, table.c.kind == "blob",
        table.c.content["sha256"].astext == content["sha256"],
    )).mappings().first()
    if existing:
        return dict(existing)
    artifact = create(conn, "artifact", project_id=project_id, kind="blob", content=content,
                      created_by_execution_id=execution_id)
    create(conn, "artifact_location", artifact_id=artifact["id"],
           locator=str(store.path(content["sha256"])), verified_at=func.now())
    return artifact
