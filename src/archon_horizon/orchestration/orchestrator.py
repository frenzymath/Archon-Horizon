"""The collaboration-round driver.

One concrete, engine-agnostic orchestrator. It depends only on the abstract
agents, scheduler, sync coordinator, lock manager, and stores — never on a
harness or a filesystem directly. The fixed sync boundaries from
:mod:`archon_horizon.core.sessions` are sequenced here and nowhere else, so
runs stay reproducible: accepted inbox items are observed only at those
boundaries, never injected into a running agent.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from archon_horizon.agents.base import HorizonAgent, HorizonContext, InformalAgent, InformalContext, InformalUpdate
from archon_horizon.core.clock import utc_now
from archon_horizon.core.events import Event
from archon_horizon.core.freeze import FreezeSet, frozen_violations
from archon_horizon.core.inbox import InboxItem
from archon_horizon.core.labels import is_accepted
from archon_horizon.core.sessions import RunRecord, SyncBoundary
from archon_horizon.core.tasks import HorizonResult, HorizonTask, TaskStatus
from archon_horizon.core.workspace import Workspace
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.store.base import EventLog, MemoryStore, ProposalStore, RoadmapStore, TaskStore

from .locks import LockManager
from .scheduler import Scheduler
from .sync import SyncCoordinator


@dataclass(frozen=True, slots=True)
class RoundReport:
    round_index: int
    tasks_run: tuple[str, ...] = ()
    tasks_blocked: tuple[str, ...] = ()


@dataclass(slots=True)
class Orchestrator:
    workspace: Workspace
    informal: InformalAgent
    horizon: HorizonAgent
    scheduler: Scheduler
    sync: SyncCoordinator
    locks: LockManager
    event_log: EventLog
    roadmap_store: RoadmapStore
    memory_store: MemoryStore
    task_store: TaskStore
    proposal_store: ProposalStore
    inbox_providers: Sequence[InboxProvider] = ()
    freeze: FreezeSet = field(default_factory=FreezeSet)

    # ── event helper ────────────────────────────────────────────────

    def _emit(self, type: str, actor: str = "orchestrator", **data: object) -> None:
        self.event_log.append(Event(type=type, id=uuid.uuid4().hex, actor=actor, data=data))

    # ── context assembly ────────────────────────────────────────────

    def _accepted_inbox(self) -> tuple[InboxItem, ...]:
        items: list[InboxItem] = []
        for provider in self.inbox_providers:
            for item in provider.list_items():
                if is_accepted(item.labels):
                    items.append(item)
        return tuple(items)

    def _informal_context(self, run: RunRecord) -> InformalContext:
        return InformalContext(
            workspace=self.workspace,
            run=run,
            focus=run.focus,
            roadmap=self.roadmap_store.load(),
            accepted_inbox=self._accepted_inbox(),
            memory=self.memory_store.load(),
        )

    def _horizon_context(self, run: RunRecord, task: HorizonTask) -> HorizonContext:
        roadmap = self.roadmap_store.load()
        return HorizonContext(
            workspace=self.workspace,
            run=run,
            task=task,
            roadmap=roadmap.slice_for_projects({task.project}),
            accepted_inbox=self._accepted_inbox(),
            memory=self.memory_store.load(),
        )

    # ── applying an informal update ─────────────────────────────────

    def _apply_informal(self, update: InformalUpdate) -> None:
        if update.roadmap is not None:
            self.roadmap_store.save(update.roadmap)
            self._emit("roadmap.updated", actor="informal")
        if update.memory is not None:
            self.memory_store.save(update.memory)
            self._emit("memory.updated", actor="informal")
        for task in update.tasks:
            stored = self.task_store.put(task)
            self._emit("task.created", actor="informal", task_id=stored.id, project=stored.project)
        for proposal in update.proposals:
            stored_p = self.proposal_store.put(proposal)
            self._emit("proposal.created", actor="informal", proposal_id=stored_p.id)

    # ── one Horizon task ────────────────────────────────────────────

    def _run_task(self, run: RunRecord, task: HorizonTask) -> bool:
        """Run one task through dispatch. Returns True if it actually ran."""
        self.sync.sync(SyncBoundary.BEFORE_HORIZON)

        violations = frozen_violations(task.write_set, self.freeze)
        if violations:
            self.task_store.put(dataclasses.replace(task, status=TaskStatus.BLOCKED, updated_at=utc_now()))
            self._emit("task.blocked", task_id=task.id, reason="freeze",
                       rules=[r.pattern for r in violations])
            return False

        if not self.locks.acquire(task.id, task.write_set):
            self._emit("task.deferred", task_id=task.id, reason="lock-conflict")
            return False

        try:
            self.task_store.put(dataclasses.replace(task, status=TaskStatus.RUNNING, updated_at=utc_now()))
            self._emit("task.started", actor="horizon", task_id=task.id)

            result = self.horizon.run_task(self._horizon_context(run, task))
            self.task_store.put(dataclasses.replace(task, status=result.status, updated_at=utc_now()))
            self._emit("task.finished", actor="horizon", task_id=task.id, status=result.status)

            boundary = (
                SyncBoundary.ON_BUILD_FAILURE
                if result.status is TaskStatus.FAILED
                else SyncBoundary.AFTER_HORIZON
            )
            self.sync.sync(boundary)

            followup = self.informal.handle_horizon_result(self._informal_context(run), result)
            self._apply_informal(followup)
            return True
        finally:
            self.locks.release(task.id)

    # ── one round / full run ────────────────────────────────────────

    def run_round(self, run: RunRecord, index: int) -> RoundReport:
        self.sync.sync(SyncBoundary.BEFORE_INFORMAL)
        self._apply_informal(self.informal.run_round(self._informal_context(run)))
        self.sync.sync(SyncBoundary.AFTER_INFORMAL)

        selected = self.scheduler.select_tasks(
            self.workspace, run, run.focus, self.task_store.list()
        )

        ran: list[str] = []
        blocked: list[str] = []
        for task in selected:
            (ran if self._run_task(run, task) else blocked).append(task.id)

        self.sync.sync(SyncBoundary.BEFORE_PUBLISH)
        self.publish(run)
        return RoundReport(round_index=index, tasks_run=tuple(ran), tasks_blocked=tuple(blocked))

    def run(self, run: RunRecord) -> list[RoundReport]:
        self._emit("run.started", run_id=run.id, rounds=run.rounds_requested)
        reports = [self.run_round(run, i) for i in range(run.rounds_requested)]
        self._emit("run.finished", run_id=run.id)
        return reports

    def publish(self, run: RunRecord) -> None:
        """Refresh dashboard/GitHub artifacts. Override to wire a publisher."""
