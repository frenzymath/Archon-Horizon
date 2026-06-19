"""Parse harness output into structured updates.

The informal agent is expected to end its output with a fenced ```json
block describing what changed; the prose before it is the human report. This
parser is intentionally forgiving: a missing or malformed block degrades to
"report only, no structured edits" rather than raising. ID minting for new
tasks/proposals is left to the store, so emitted items may carry ``id=""``.
"""

from __future__ import annotations

import json
from typing import Any

from archon_horizon.core.tasks import HorizonTask, Proposal, WriteSet

from .base import InformalUpdate

_FENCE = "```"


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Return the last ```json fenced object in ``text``, if any."""
    marker = _FENCE + "json"
    start = text.rfind(marker)
    if start == -1:
        return None
    body_start = start + len(marker)
    end = text.find(_FENCE, body_start)
    raw = text[body_start:] if end == -1 else text[body_start:end]
    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _task_from_dict(data: dict[str, Any]) -> HorizonTask | None:
    project = data.get("project")
    objective = data.get("objective")
    if not isinstance(project, str) or not isinstance(objective, str):
        return None
    write = data.get("write_set") or {}
    return HorizonTask(
        id=str(data.get("id") or ""),
        project=project,
        objective=objective,
        write_set=WriteSet(
            files=tuple(write.get("files", ())),
            projects=tuple(write.get("projects", ())),
            workspace=bool(write.get("workspace", False)),
        ),
        roadmap_refs=tuple(data.get("roadmap_refs", ())),
        inbox_refs=tuple(data.get("inbox_refs", ())),
    )


def _proposal_from_dict(data: dict[str, Any]) -> Proposal | None:
    title = data.get("title")
    body = data.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        return None
    return Proposal(id=str(data.get("id") or ""), title=title, body=body)


def parse_informal_update(text: str) -> InformalUpdate:
    block = _extract_json_block(text)
    if block is None:
        return InformalUpdate(report=text.strip())

    tasks = tuple(t for t in map(_task_from_dict, block.get("tasks", [])) if t is not None)
    proposals = tuple(
        p for p in map(_proposal_from_dict, block.get("proposals", [])) if p is not None
    )
    memory = block.get("memory")
    return InformalUpdate(
        report=text[: text.rfind(_FENCE + "json")].strip() or text.strip(),
        memory=memory if isinstance(memory, str) else None,
        tasks=tasks,
        proposals=proposals,
    )
