"""Parse harness output into structured informal updates.

The informal agent ends its response with a fenced `````json`` object. The
prose before it is the report. Parsing is intentionally forgiving: invalid or
missing structured data produces a report-only update instead of crashing the
round. This keeps the harness usable with real agents while preserving a clear
machine contract for first-version automation.
"""

from __future__ import annotations

import json
from typing import Any

from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxScope
from archon_horizon.core.labels import ARCHON_PENDING
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapKind, RoadmapStatus
from archon_horizon.core.tasks import HorizonTask, Proposal, WriteSet

from .base import InformalUpdate

_FENCE = "```"


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Return the last fenced json object in ``text``, if any."""
    marker = _FENCE + "json"
    start = text.rfind(marker)
    if start == -1:
        return None
    body_start = start + len(marker)
    end = text.find(_FENCE, body_start)
    raw = text[body_start:] if end == -1 else text[body_start:end]
    try:
        parsed = json.loads(raw.strip() or "{}")
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str))


def _task_from_dict(data: dict[str, Any]) -> HorizonTask | None:
    project = data.get("project")
    objective = data.get("objective")
    if not isinstance(project, str) or not isinstance(objective, str):
        return None
    write = data.get("write_set") if isinstance(data.get("write_set"), dict) else {}
    return HorizonTask(
        id=str(data.get("id") or ""),
        project=project,
        objective=objective,
        write_set=WriteSet(
            files=_strings(write.get("files", ())),
            projects=_strings(write.get("projects", ())),
            workspace=bool(write.get("workspace", False)),
        ),
        roadmap_refs=_strings(data.get("roadmap_refs", ())),
        inbox_refs=_strings(data.get("inbox_refs", ())),
        metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), dict) else {},
    )


def _proposal_from_dict(data: dict[str, Any]) -> Proposal | None:
    title = data.get("title")
    body = data.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    return Proposal(
        id=str(data.get("id") or ""),
        title=title,
        body=body,
        project=data.get("project") if isinstance(data.get("project"), str) else None,
        inbox_refs=_strings(data.get("inbox_refs", ())),
        artifact_refs=_strings(data.get("artifact_refs", ())),
        metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), dict) else {},
    )


def _roadmap_item_from_dict(data: dict[str, Any]) -> RoadmapItem | None:
    item_id = data.get("id")
    title = data.get("title")
    if not isinstance(item_id, str) or not isinstance(title, str):
        return None
    try:
        status = RoadmapStatus(data.get("status", RoadmapStatus.PENDING))
    except ValueError:
        status = RoadmapStatus.PENDING
    try:
        kind = RoadmapKind(data.get("kind", RoadmapKind.PROOF))
    except ValueError:
        kind = RoadmapKind.PROOF
    return RoadmapItem(
        id=item_id,
        title=title,
        projects=_strings(data.get("projects", ())),
        summary=data.get("summary", "") if isinstance(data.get("summary", ""), str) else "",
        status=status,
        kind=kind,
        priority=data.get("priority", "normal") if isinstance(data.get("priority", "normal"), str) else "normal",
        depends_on=_strings(data.get("depends_on", ())),
        inbox_refs=_strings(data.get("inbox_refs", ())),
        task_refs=_strings(data.get("task_refs", ())),
        metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), dict) else {},
    )


def _roadmap_from_obj(value: Any) -> Roadmap | None:
    if isinstance(value, list):
        raw_items = value
        version = 1
    elif isinstance(value, dict):
        raw_items = value.get("items", [])
        version = int(value.get("version", 1) or 1)
    else:
        return None
    if not isinstance(raw_items, list):
        return None
    items = tuple(
        item for item in (_roadmap_item_from_dict(x) for x in raw_items if isinstance(x, dict))
        if item is not None
    )
    return Roadmap(version=version, items=items)


def _issue_from_dict(data: dict[str, Any]) -> InboxDraft | None:
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        return None
    try:
        kind = InboxKind(data.get("kind", InboxKind.ISSUE))
    except ValueError:
        kind = InboxKind.ISSUE
    labels = _strings(data.get("labels", ())) or (ARCHON_PENDING,)
    scope = InboxScope(
        project=data.get("project") if isinstance(data.get("project"), str) else None,
        file=data.get("file") if isinstance(data.get("file"), str) else None,
        declaration=data.get("declaration") if isinstance(data.get("declaration"), str) else None,
    )
    return InboxDraft(
        kind=kind,
        body=body,
        labels=labels,
        scope=scope,
        source_ref=data.get("source_ref") if isinstance(data.get("source_ref"), str) else None,
        metadata=dict(data.get("metadata", {})) if isinstance(data.get("metadata"), dict) else {},
    )


def parse_informal_update(text: str) -> InformalUpdate:
    block = _extract_json_block(text)
    if block is None:
        return InformalUpdate(report=text.strip())

    tasks = tuple(
        t for t in (_task_from_dict(x) for x in block.get("tasks", []) if isinstance(x, dict))
        if t is not None
    )
    proposals = tuple(
        p for p in (_proposal_from_dict(x) for x in block.get("proposals", []) if isinstance(x, dict))
        if p is not None
    )
    local_issues = tuple(
        i for i in (_issue_from_dict(x) for x in block.get("local_issues", []) if isinstance(x, dict))
        if i is not None
    )
    memory = block.get("memory")
    roadmap = _roadmap_from_obj(block.get("roadmap")) if "roadmap" in block else None
    return InformalUpdate(
        report=text[: text.rfind(_FENCE + "json")].strip() or text.strip(),
        roadmap=roadmap,
        memory=memory if isinstance(memory, str) else None,
        tasks=tasks,
        proposals=proposals,
        local_issues=local_issues,
    )
