"""Serialization between core dataclasses and plain dicts.

Encoding is generic (any frozen dataclass -> jsonable dict). Decoding is
explicit per type: it is the one place that knows a stored dict is a
``HorizonTask`` versus a ``RoadmapItem``, so the core dataclasses stay pure
contracts with no awareness of disk format. Keeping serde out of ``core``
preserves the dependency rule: ``core`` imports nothing outward.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from archon_horizon.core.events import Event
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxStatus
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapKind, RoadmapStatus
from archon_horizon.core.scope import ItemScope, compact_scope, scope_from_dict
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import (
    HorizonTask,
    TaskStatus,
    WriteSet,
)


# Types that are already JSON scalars and can never be a dataclass, Enum,
# datetime, Path, or container. Matched by EXACT type, so an `Enum` that
# subclasses `str`/`int` (e.g. `class TaskStatus(str, Enum)`) still falls
# through to the Enum branch below and serializes as its value.
_JSON_SCALARS = frozenset({str, int, float, bool, type(None)})


def to_jsonable(obj: Any) -> Any:
    # Scalars dominate by a wide margin — event payloads are mostly strings and
    # numbers, and a large workspace's `state()` recurses here ~700k times per
    # call. Settling them in one exact-type hash lookup, before the isinstance
    # ladder, is worth the early return.
    if type(obj) in _JSON_SCALARS:
        return obj
    if isinstance(obj, ItemScope):
        return {k: to_jsonable(v) for k, v in compact_scope(obj).items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    return obj


def _dt(value: Any) -> datetime:
    return datetime.fromisoformat(value)


def event_from_dict(data: dict[str, Any]) -> Event:
    return Event(
        type=data["type"],
        id=data["id"],
        created_at=_dt(data["created_at"]),
        actor=data.get("actor", "system"),
        data=dict(data.get("data", {})),
    )


def write_set_from_dict(data: dict[str, Any]) -> WriteSet:
    return WriteSet(
        files=tuple(data.get("files", ())),
        projects=tuple(data.get("projects", ())),
        declarations=tuple(data.get("declarations", ())),
        blueprint_nodes=tuple(data.get("blueprint_nodes", ())),
        workspace=bool(data.get("workspace", False)),
    )


def task_from_dict(data: dict[str, Any]) -> HorizonTask:
    objective = data.get("objective", data.get("explanation", data.get("title", "")))
    write_set = write_set_from_dict(data.get("write_set", {}))
    scope = scope_from_dict(data.get("scope") or {})
    return HorizonTask(
        id=data["id"],
        project=data["project"],
        objective=objective,
        title=data.get("title", ""),
        explanation=data.get("explanation", ""),
        projects=tuple(data.get("projects", ())),
        priority=data.get("priority", "normal"),
        status=TaskStatus(data.get("status", TaskStatus.QUEUED)),
        write_set=write_set,
        scope=scope,
        roadmap_refs=tuple(data.get("roadmap_refs", ())),
        inbox_refs=tuple(data.get("inbox_refs", ())),
        artifact_refs=tuple(data.get("artifact_refs", ())),
        created_at=_dt(data["created_at"]),
        updated_at=_dt(data["updated_at"]),
        metadata=dict(data.get("metadata", {})),
    )


def roadmap_item_from_dict(data: dict[str, Any]) -> RoadmapItem:
    scope = scope_from_dict(data.get("scope") or {})
    return RoadmapItem(
        id=data["id"],
        title=data["title"],
        projects=tuple(data.get("projects", ())),
        summary=data.get("summary", ""),
        status=RoadmapStatus(data.get("status", RoadmapStatus.PENDING)),
        kind=RoadmapKind(data.get("kind", RoadmapKind.PROOF)),
        priority=data.get("priority", "normal"),
        depends_on=tuple(data.get("depends_on", ())),
        inbox_refs=tuple(data.get("inbox_refs", ())),
        task_refs=tuple(data.get("task_refs", ())),
        scope=scope,
        metadata=dict(data.get("metadata", {})),
    )


def _inbox_status(value: Any) -> InboxStatus:
    # Forward-compatible: an unrecognized status maps to closed (only "open" is
    # open), so a status a future version adds can't break older readers.
    try:
        return InboxStatus(value)
    except ValueError:
        return InboxStatus.OPEN if str(value) == "open" else InboxStatus.CLOSED


def _inbox_kind(value: Any) -> InboxKind:
    # Forward-compatible: an unknown kind falls back to a plain hint so a single
    # item written by a newer version can't take down the whole dashboard.
    try:
        return InboxKind(value)
    except ValueError:
        return InboxKind.HINT


def inbox_item_from_dict(data: dict[str, Any]) -> InboxItem:
    scope = scope_from_dict(data.get("scope") or {})
    metadata = dict(data.get("metadata", {}))
    kind = _inbox_kind(data["kind"])
    # Read-time migration: threads created before `conversation` became a kind
    # retain their routing metadata but now participate in first-class filters
    # and priority ordering. Older Horizon versions still fall back safely to a
    # hint when they encounter the new value on disk.
    if metadata.get("conversation"):
        kind = InboxKind.CONVERSATION
    return InboxItem(
        id=data["id"],
        provider=data["provider"],
        kind=kind,
        body=data["body"],
        labels=tuple(data.get("labels", ())),
        status=_inbox_status(data.get("status", InboxStatus.OPEN)),
        scope=scope,
        audience=data.get("audience", ""),
        author=data.get("author", ""),
        source_ref=data.get("source_ref"),
        created_at=_dt(data["created_at"]),
        updated_at=_dt(data["updated_at"]),
        metadata=metadata,
    )


def roadmap_from_dict(data: dict[str, Any]) -> Roadmap:
    return Roadmap(
        version=data.get("version", 1),
        updated_at=_dt(data["updated_at"]),
        items=tuple(roadmap_item_from_dict(item) for item in data.get("items", ())),
    )


def run_record_from_dict(data: dict[str, Any]) -> RunRecord:
    focus = data.get("focus") or {}
    return RunRecord(
        id=data["id"],
        focus=Focus(
            projects=tuple(focus.get("projects", ())),
            task=focus.get("task"),
            tasks=tuple(focus.get("tasks", ())),
        ),
        rounds_requested=int(data.get("rounds_requested", 1)),
        created_at=_dt(data["created_at"]),
        metadata=dict(data.get("metadata", {})),
    )
