"""Serialization between core dataclasses and plain dicts.

Encoding is generic (any frozen dataclass -> jsonable dict). Decoding is
explicit per type: it is the one place that knows a stored dict is a
``HorizonTask`` versus a ``Proposal``, so the core dataclasses stay pure
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
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxScope, InboxStatus
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapKind, RoadmapStatus
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import (
    HorizonTask,
    Proposal,
    ProposalStatus,
    TaskStatus,
    WriteSet,
)


def to_jsonable(obj: Any) -> Any:
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
        workspace=bool(data.get("workspace", False)),
    )


def task_from_dict(data: dict[str, Any]) -> HorizonTask:
    return HorizonTask(
        id=data["id"],
        project=data["project"],
        objective=data["objective"],
        status=TaskStatus(data.get("status", TaskStatus.QUEUED)),
        write_set=write_set_from_dict(data.get("write_set", {})),
        roadmap_refs=tuple(data.get("roadmap_refs", ())),
        inbox_refs=tuple(data.get("inbox_refs", ())),
        artifact_refs=tuple(data.get("artifact_refs", ())),
        created_at=_dt(data["created_at"]),
        updated_at=_dt(data["updated_at"]),
        metadata=dict(data.get("metadata", {})),
    )


def proposal_from_dict(data: dict[str, Any]) -> Proposal:
    return Proposal(
        id=data["id"],
        title=data["title"],
        body=data["body"],
        status=ProposalStatus(data.get("status", ProposalStatus.DRAFT)),
        project=data.get("project"),
        inbox_refs=tuple(data.get("inbox_refs", ())),
        artifact_refs=tuple(data.get("artifact_refs", ())),
        created_at=_dt(data["created_at"]),
        updated_at=_dt(data["updated_at"]),
        metadata=dict(data.get("metadata", {})),
    )


def roadmap_item_from_dict(data: dict[str, Any]) -> RoadmapItem:
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
        metadata=dict(data.get("metadata", {})),
    )


def inbox_item_from_dict(data: dict[str, Any]) -> InboxItem:
    scope = data.get("scope") or {}
    return InboxItem(
        id=data["id"],
        provider=data["provider"],
        kind=InboxKind(data["kind"]),
        body=data["body"],
        labels=tuple(data.get("labels", ())),
        status=InboxStatus(data.get("status", InboxStatus.OPEN)),
        scope=InboxScope(
            project=scope.get("project"),
            file=scope.get("file"),
            declaration=scope.get("declaration"),
        ),
        source_ref=data.get("source_ref"),
        created_at=_dt(data["created_at"]),
        updated_at=_dt(data["updated_at"]),
        metadata=dict(data.get("metadata", {})),
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
            proposal=focus.get("proposal"),
        ),
        rounds_requested=int(data.get("rounds_requested", 1)),
        created_at=_dt(data["created_at"]),
        metadata=dict(data.get("metadata", {})),
    )
