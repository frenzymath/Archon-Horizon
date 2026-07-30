"""Shared status policies for tasks and roadmap items."""

from __future__ import annotations

from archon_horizon.core.roadmap import RoadmapStatus
from archon_horizon.core.tasks import HorizonTask, TaskStatus


TASK_TO_ROADMAP_STATUS = {
    TaskStatus.DONE: RoadmapStatus.DONE,
    TaskStatus.BLOCKED: RoadmapStatus.BLOCKED,
    TaskStatus.FAILED: RoadmapStatus.BLOCKED,
    TaskStatus.CANCELLED: RoadmapStatus.REJECTED,
}

TASK_TERMINAL_STATUSES = frozenset(TASK_TO_ROADMAP_STATUS)


def roadmap_status_for_task_status(status: TaskStatus) -> RoadmapStatus | None:
    """Return the roadmap status that matches a terminal task status."""
    return TASK_TO_ROADMAP_STATUS.get(status)


def has_recorded_terminal_status(task: HorizonTask) -> bool:
    """Whether a task has an explicit history transition to its terminal status.

    Orchestrator-owned Horizon results update task files directly, without
    appending a user/CLI history entry. Dashboard and CLI edits do append
    history. Use ``terminal_status_actor`` when orchestration needs to distinguish
    an agent-owned completion from an external close/cancel.
    """
    return terminal_status_actor(task) is not None


def terminal_status_actor(task: HorizonTask) -> str | None:
    """Return the actor that recorded the task's current terminal transition.

    ``None`` means the status is not terminal or the transition has no history
    entry. The actor lets orchestration distinguish an agent's own final
    declaration from an external human close while the harness is still live.
    """
    if task.status not in TASK_TERMINAL_STATUSES:
        return None
    history = task.metadata.get("history")
    if not isinstance(history, list):
        return None
    for entry in reversed(history):
        if not isinstance(entry, dict):
            continue
        if entry.get("field") == "status" and entry.get("to") == task.status.value:
            actor = str(entry.get("actor") or "").strip().lower()
            return actor or "system"
    return None
