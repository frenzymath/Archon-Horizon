"""Scheduler contract and a freeze/lock-aware default."""

from __future__ import annotations

from abc import ABC, abstractmethod

from archon_horizon.core.freeze import FreezeSet, frozen_violations
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus
from archon_horizon.core.workspace import Workspace

from .locks import write_sets_conflict

_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


def _priority_key(task: HorizonTask) -> tuple[int, str]:
    raw = task.metadata.get("roadmap_priority", task.metadata.get("priority", "normal"))
    return (_PRIORITY_ORDER.get(str(raw).lower(), _PRIORITY_ORDER["normal"]), task.id)


class Scheduler(ABC):
    """Chooses Horizon tasks while respecting focus and write locks."""

    @abstractmethod
    def select_tasks(
        self,
        workspace: Workspace,
        run: RunRecord,
        focus: Focus,
        candidates: list[HorizonTask],
    ) -> list[HorizonTask]:
        """Return the tasks that may run now."""


class FreezeAwareScheduler(Scheduler):
    """Pick runnable, non-conflicting, non-frozen tasks within the focus.

    Selection is deterministic (candidate order is preserved). Frozen tasks
    are dropped here as a safety net; the orchestrator still re-checks freeze
    immediately before dispatch, since freeze can change between rounds.
    """

    def __init__(self, *, freeze: FreezeSet | None = None, max_parallel: int = 1) -> None:
        self._freeze = freeze or FreezeSet()
        self._max_parallel = max_parallel

    def _in_focus(self, task: HorizonTask, focus: Focus) -> bool:
        if focus.tasks:
            return task.id in focus.tasks
        if focus.task is not None:
            return task.id == focus.task
        if focus.projects:
            return task.project in focus.projects
        return True

    def select_tasks(
        self,
        workspace: Workspace,
        run: RunRecord,
        focus: Focus,
        candidates: list[HorizonTask],
    ) -> list[HorizonTask]:
        selected: list[HorizonTask] = []
        for task in sorted(candidates, key=_priority_key):
            if task.status is not TaskStatus.QUEUED:
                continue
            if not self._in_focus(task, focus):
                continue
            if frozen_violations(task.write_set, self._freeze):
                continue
            if any(write_sets_conflict(task.write_set, s.write_set) for s in selected):
                continue
            selected.append(task)
            if len(selected) >= self._max_parallel:
                break
        return selected
