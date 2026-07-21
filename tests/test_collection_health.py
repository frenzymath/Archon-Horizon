"""Advisory health warnings for roadmap, task, and inbox collections."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from archon_horizon.core.collection_health import (
    AdvisoryHealthLimits,
    inbox_health_warnings,
    roadmap_health_warnings,
    task_health_warnings,
)
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxStatus
from archon_horizon.core.roadmap import RoadmapItem, RoadmapStatus
from archon_horizon.core.tasks import HorizonTask, TaskStatus


LIMITS = AdvisoryHealthLimits(
    open_memories=2,
    open_info=1,
    open_inbox=3,
    open_tasks=2,
    active_roadmap=2,
    stale_running_task_hours=24,
)


def _inbox_item(index: int, kind: InboxKind, status: InboxStatus = InboxStatus.OPEN) -> InboxItem:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return InboxItem(
        id=f"I-{index:04d}",
        provider="local",
        kind=kind,
        body="title\n\ndescription",
        labels=("agent-ready",),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _task(index: int, status: TaskStatus, updated_at: datetime) -> HorizonTask:
    return HorizonTask(
        id=f"T-{index}",
        project="p",
        objective="test",
        status=status,
        updated_at=updated_at,
    )


def _roadmap_item(index: int, status: RoadmapStatus) -> RoadmapItem:
    return RoadmapItem(id=f"R-{index}", title="test", projects=("p",), status=status)


def test_inbox_health_warns_by_kind_and_total_but_excludes_protections() -> None:
    items = [
        *(_inbox_item(i, InboxKind.MEMORY) for i in range(3)),
        *(_inbox_item(i + 10, InboxKind.INFO) for i in range(2)),
        _inbox_item(20, InboxKind.PROTECTION),
        _inbox_item(21, InboxKind.MEMORY, InboxStatus.ARCHIVED),
    ]
    warnings = inbox_health_warnings(items, LIMITS)
    assert len(warnings) == 3
    assert "3 open memory" in warnings[0]
    assert "2 open info" in warnings[1]
    assert "5 open non-protection" in warnings[2]


def test_task_health_warns_for_queue_size_and_stale_running_status() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    tasks = [
        _task(1, TaskStatus.QUEUED, now),
        _task(2, TaskStatus.BLOCKED, now),
        _task(3, TaskStatus.RUNNING, now - timedelta(hours=25)),
        _task(4, TaskStatus.DONE, now - timedelta(days=20)),
    ]
    warnings = task_health_warnings(tasks, LIMITS, now=now)
    assert len(warnings) == 2
    assert "3 open tasks" in warnings[0]
    assert "T-3" in warnings[1] and "more than 24h" in warnings[1]


def test_task_health_accepts_legacy_naive_timestamps() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    stale_naive = datetime(2026, 1, 1, 11)
    warnings = task_health_warnings(
        [_task(1, TaskStatus.RUNNING, stale_naive)], LIMITS, now=now
    )
    assert len(warnings) == 1 and "T-1" in warnings[0]


def test_roadmap_health_counts_only_active_items() -> None:
    items = [
        _roadmap_item(1, RoadmapStatus.ACTIVE),
        _roadmap_item(2, RoadmapStatus.ACTIVE),
        _roadmap_item(3, RoadmapStatus.ACTIVE),
        _roadmap_item(4, RoadmapStatus.PENDING),
    ]
    warnings = roadmap_health_warnings(items, LIMITS)
    assert len(warnings) == 1
    assert "3 active items" in warnings[0]


def test_collections_at_the_limit_are_quiet() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
    assert inbox_health_warnings(
        [_inbox_item(1, InboxKind.MEMORY), _inbox_item(2, InboxKind.MEMORY)], LIMITS
    ) == []
    assert task_health_warnings(
        [_task(1, TaskStatus.QUEUED, now), _task(2, TaskStatus.BLOCKED, now)],
        LIMITS,
        now=now,
    ) == []
    assert roadmap_health_warnings(
        [_roadmap_item(1, RoadmapStatus.ACTIVE), _roadmap_item(2, RoadmapStatus.ACTIVE)],
        LIMITS,
    ) == []
