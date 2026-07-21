"""Advisory health checks for the human/agent-maintained work queues.

These checks deliberately never mutate state or fail a command.  They make the
workspace-hygiene guidance executable and visible to every CLI caller while
leaving room for a human or agent to decide that an unusually large collection
is intentional.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .clock import utc_now
from .inbox import InboxItem, InboxKind, InboxStatus
from .roadmap import RoadmapItem, RoadmapStatus
from .tasks import HorizonTask, TaskStatus


@dataclass(frozen=True, slots=True)
class AdvisoryHealthLimits:
    """Central defaults for CLI collection-health warnings."""

    open_memories: int = 10
    open_info: int = 4
    open_inbox: int = 30
    open_tasks: int = 12
    active_roadmap: int = 8
    stale_running_task_hours: int = 24


DEFAULT_HEALTH_LIMITS = AdvisoryHealthLimits()


def _as_utc(value: datetime) -> datetime:
    """Normalize legacy naive timestamps before comparing their age."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def inbox_health_warnings(
    items: Sequence[InboxItem], limits: AdvisoryHealthLimits = DEFAULT_HEALTH_LIMITS
) -> list[str]:
    """Warnings for an inbox whose open working set may need pruning."""
    open_items = [item for item in items if item.status is InboxStatus.OPEN]
    warnings: list[str] = []
    counts = {
        kind: sum(item.kind is kind for item in open_items)
        for kind in (InboxKind.MEMORY, InboxKind.INFO)
    }
    if counts[InboxKind.MEMORY] > limits.open_memories:
        warnings.append(
            f"Inbox has {counts[InboxKind.MEMORY]} open memory items "
            f"(recommended maximum {limits.open_memories}) — archive stale, "
            "superseded, or no-longer-key memories if this is not intentional."
        )
    if counts[InboxKind.INFO] > limits.open_info:
        warnings.append(
            f"Inbox has {counts[InboxKind.INFO]} open info items "
            f"(recommended maximum {limits.open_info}) — archive notices that "
            "have been consumed if this is not intentional."
        )
    working_set = [item for item in open_items if item.kind is not InboxKind.PROTECTION]
    if len(working_set) > limits.open_inbox:
        warnings.append(
            f"Inbox has {len(working_set)} open non-protection items "
            f"(recommended maximum {limits.open_inbox}) — review duplicates and "
            "stale items; use `inbox complete` or `inbox archive` where appropriate."
        )
    return warnings


def task_health_warnings(
    tasks: Sequence[HorizonTask],
    limits: AdvisoryHealthLimits = DEFAULT_HEALTH_LIMITS,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Warnings for an oversized task queue or apparently orphaned runs."""
    open_statuses = {TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.BLOCKED}
    open_tasks = [task for task in tasks if task.status in open_statuses]
    warnings: list[str] = []
    if len(open_tasks) > limits.open_tasks:
        warnings.append(
            f"Task queue has {len(open_tasks)} open tasks "
            f"(recommended maximum {limits.open_tasks}) — review whether some "
            "objectives are done, blocked, cancelled, duplicated, or better kept "
            "only as roadmap milestones."
        )

    cutoff = _as_utc(now or utc_now()) - timedelta(hours=limits.stale_running_task_hours)
    stale = [
        task for task in tasks
        if task.status is TaskStatus.RUNNING and _as_utc(task.updated_at) < cutoff
    ]
    if stale:
        shown = ", ".join(task.id for task in stale[:4])
        more = f" (+{len(stale) - 4} more)" if len(stale) > 4 else ""
        warnings.append(
            f"Task(s) {shown}{more} have remained running for more than "
            f"{limits.stale_running_task_hours}h — confirm a run is still live or "
            "reset their status if it ended unexpectedly."
        )
    return warnings


def roadmap_health_warnings(
    items: Sequence[RoadmapItem], limits: AdvisoryHealthLimits = DEFAULT_HEALTH_LIMITS
) -> list[str]:
    """Warnings when too many roadmap milestones claim simultaneous focus."""
    active = [item for item in items if item.status is RoadmapStatus.ACTIVE]
    if len(active) <= limits.active_roadmap:
        return []
    return [
        f"Roadmap has {len(active)} active items "
        f"(recommended maximum {limits.active_roadmap}) — consider marking "
        "deferred work pending, or leave the broader focus active intentionally."
    ]
