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
import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from archon_horizon.agents.base import HorizonAgent, HorizonContext, InformalAgent, InformalContext, InformalUpdate
from archon_horizon.blueprint.workspace import workspace_dags
from archon_horizon.core.clock import utc_now
from archon_horizon.core.events import Event
from archon_horizon.core.freeze import FreezeSet, frozen_violations
from archon_horizon.core.inbox import InboxDraft, InboxItem
from archon_horizon.core.labels import ARCHON_PENDING, is_accepted
from archon_horizon.core.sessions import RunRecord, SyncBoundary
from archon_horizon.core.tasks import HorizonResult, HorizonTask, ProposalStatus, TaskStatus, WriteSet
from archon_horizon.core.workspace import Workspace
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.render.roadmap_md import render_roadmap_markdown
from archon_horizon.runlog import RunLog, RunLogTree, SessionLog
from archon_horizon.subagents.base import Subagent, SubagentContext
from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.sink import JsonlTranscriptSink
from archon_horizon.store.base import (
    EventLog,
    MemoryStore,
    ProposalStore,
    ReportStore,
    RoadmapStore,
    RunStore,
    TaskStore,
)
from archon_horizon.vcs.integration import integrate_workspace_session, project_checkpoint

from .locks import LockManager
from .scheduler import Scheduler
from .sync import SyncCoordinator


@dataclass(frozen=True, slots=True)
class RoundReport:
    round_index: int
    tasks_run: tuple[str, ...] = ()
    tasks_blocked: tuple[str, ...] = ()
    planned: tuple[str, ...] = ()


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
    report_store: ReportStore | None = None
    run_store: RunStore | None = None
    run_logs: RunLogTree | None = None
    informal_subagents: tuple[Subagent, ...] = ()
    freeze: FreezeSet = field(default_factory=FreezeSet)

    # ── event helper ────────────────────────────────────────────────

    def _emit(self, type: str, actor: str = "orchestrator", **data: object) -> None:
        self.event_log.append(Event(type=type, id=uuid.uuid4().hex, actor=actor, data=data))

    @staticmethod
    def _session(runlog: RunLog | None, label: str) -> SessionLog | None:
        return runlog.new_session(label) if runlog is not None else None

    @staticmethod
    def _log_dir(session: SessionLog | None) -> Path | None:
        return session.path if session is not None else None

    def _integrate_session(
        self,
        run: RunRecord,
        session: SessionLog | None,
        *,
        role: str,
        project: str | None = None,
        task_id: str | None = None,
        project_commits: dict[str, str | None] | None = None,
        project_commit_errors: dict[str, str] | None = None,
    ) -> None:
        if session is None:
            return
        integration = integrate_workspace_session(
            self.workspace,
            run_id=run.id,
            session=session.name,
            role=role,
            project=project,
            task_id=task_id,
            project_commits=project_commits,
            project_commit_errors=project_commit_errors,
        )
        self._emit(
            "workspace.session.integrated",
            run_id=run.id,
            session=session.name,
            role=role,
            project=project,
            task_id=task_id,
            project_commits=integration.project_commits,
            project_commit_errors=integration.project_commit_errors,
            workspace_commit=integration.workspace_commit,
            workspace_commit_error=integration.workspace_commit_error,
            manifest_ref=integration.manifest_ref,
        )

    # ── context assembly ────────────────────────────────────────────

    def _accepted_inbox(self) -> tuple[InboxItem, ...]:
        items: list[InboxItem] = []
        for provider in self.inbox_providers:
            for item in provider.list_items():
                if is_accepted(item.labels):
                    items.append(item)
        return tuple(items)

    def _blueprint_summary(self) -> str:
        lines = []
        for project, dag in workspace_dags(self.workspace).items():
            nodes = dag.get("nodes", [])
            proved = sum(1 for n in nodes if n.get("leanok"))
            lines.append(
                f"{project}: {len(nodes)} nodes, {proved} proved, "
                f"{len(dag.get('edges', []))} edges, {len(dag.get('dangling', []))} dangling"
            )
        return "\n".join(lines)

    def _informal_context(self, run: RunRecord, log_dir: Path | None = None) -> InformalContext:
        return InformalContext(
            workspace=self.workspace,
            run=run,
            focus=run.focus,
            roadmap=self.roadmap_store.load(),
            accepted_inbox=self._accepted_inbox(),
            memory=self.memory_store.load(),
            blueprint_summary=self._blueprint_summary(),
            log_dir=log_dir,
        )

    # ── freeze / subagents / proposals ──────────────────────────────

    def _agent_frozen(self, agent: str) -> bool:
        return self.freeze.agent_rule(agent) is not None

    def _local_provider(self) -> InboxProvider | None:
        for provider in self.inbox_providers:
            if "create" in provider.capabilities:
                return provider
        return None

    def _create_local_issues(self, drafts: Sequence[InboxDraft]) -> int:
        provider = self._local_provider()
        if provider is None or not drafts:
            return 0
        existing = {(i.kind, i.body) for i in provider.list_items()}
        created = 0
        for draft in drafts:
            if (draft.kind, draft.body) in existing:
                continue
            provider.create_item(draft)
            existing.add((draft.kind, draft.body))
            created += 1
        return created

    def _dispatch_subagents(self, parent: SessionLog | None) -> None:
        for sub in self.informal_subagents:
            session = parent.new_subsession(sub.name) if parent is not None else None
            result = sub.run(SubagentContext(self.workspace, self._log_dir(session)))
            if session is not None:
                if not session.transcript_path.exists():
                    sink = JsonlTranscriptSink(session.transcript_path)
                    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"subagent": sub.name}))
                    if result.report:
                        sink.emit(TranscriptEvent(TranscriptKind.TEXT, text=result.report))
                    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": result.ok}))
                session.write_meta({
                    "role": "subagent",
                    "name": sub.name,
                    "ok": result.ok,
                    "data": result.data,
                })
            # Subagent findings await human triage (pending), not auto-dispatch.
            drafts = tuple(dataclasses.replace(d, labels=(ARCHON_PENDING,)) for d in result.issues)
            created = self._create_local_issues(drafts)
            self._emit("subagent.ran", name=sub.name, ok=result.ok, issues_created=created)

    def _execute_proposal(self, proposal_id: str) -> None:
        if self.proposal_store is None:
            return
        proposal = self.proposal_store.get(proposal_id)
        task_spec = proposal.metadata.get("task")
        if isinstance(task_spec, dict) and task_spec.get("project") and task_spec.get("objective"):
            stored = self.task_store.put(HorizonTask(
                id="", project=task_spec["project"], objective=task_spec["objective"],
                write_set=WriteSet(files=tuple(task_spec.get("files", ()))),
            ))
            self._emit("proposal.applied", proposal_id=proposal_id, created_task=stored.id)
        self.proposal_store.put(
            dataclasses.replace(proposal, status=ProposalStatus.APPLIED, updated_at=utc_now())
        )

    def _horizon_context(
        self, run: RunRecord, task: HorizonTask, log_dir: Path | None = None
    ) -> HorizonContext:
        roadmap = self.roadmap_store.load()
        return HorizonContext(
            workspace=self.workspace,
            run=run,
            task=task,
            roadmap=roadmap.slice_for_projects({task.project}),
            accepted_inbox=self._accepted_inbox(),
            memory=self.memory_store.load(),
            log_dir=log_dir,
        )

    # ── applying an informal update ─────────────────────────────────

    def _write_report(self, name: str, text: str) -> str | None:
        if not text or self.report_store is None:
            return None
        ref = self.report_store.write(name, text)
        self._emit("report.written", name=name, ref=ref)
        return ref

    def _apply_informal(self, update: InformalUpdate, *, report_name: str = "informal") -> None:
        if update.roadmap is not None:
            self.roadmap_store.save(update.roadmap)
            self._write_report("roadmap", render_roadmap_markdown(update.roadmap))
            self._emit("roadmap.updated", actor="informal")
        if update.memory is not None:
            self.memory_store.save(update.memory)
            self._emit("memory.updated", actor="informal")
        self._write_report(report_name, update.report)
        for task in update.tasks:
            stored = self.task_store.put(task)
            self._emit("task.created", actor="informal", task_id=stored.id, project=stored.project)
        for proposal in update.proposals:
            stored_p = self.proposal_store.put(proposal)
            self._emit("proposal.created", actor="informal", proposal_id=stored_p.id)
        created_issues = self._create_local_issues(update.local_issues)
        if created_issues:
            self._emit("local_issues.created", actor="informal", count=created_issues)

    # ── one Horizon task ────────────────────────────────────────────

    def _run_task(self, run: RunRecord, task: HorizonTask, runlog: RunLog | None) -> bool:
        """Run one task through dispatch. Returns True if it actually ran."""
        self.sync.sync(SyncBoundary.BEFORE_HORIZON)

        if self._agent_frozen("horizon"):
            self.task_store.put(dataclasses.replace(task, status=TaskStatus.BLOCKED, updated_at=utc_now()))
            self._emit("task.blocked", task_id=task.id, reason="agent-freeze")
            return False

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

            session = self._session(runlog, f"horizon-{task.id}")
            started = utc_now()
            result = self.horizon.run_task(self._horizon_context(run, task, self._log_dir(session)))
            project_commits: dict[str, str | None] = {}
            project_commit_errors: dict[str, str] = {}
            checkpoint = project_checkpoint(
                self.workspace,
                task.project,
                message=f"horizon({task.project}): {task.id}\n\nRun: {run.id}\nSession: {session.name if session else 'none'}",
            )
            if checkpoint.error:
                project_commit_errors[task.project] = checkpoint.error
                self._emit("project.commit.failed", project=task.project, task_id=task.id, error=checkpoint.error)
            elif checkpoint.attempted:
                project_commits[task.project] = checkpoint.sha
                self._emit(
                    "project.commit",
                    project=task.project,
                    task_id=task.id,
                    sha=checkpoint.sha,
                    changed=checkpoint.changed,
                )
            self._write_report(f"task-{task.id}", result.report)
            self.task_store.put(dataclasses.replace(task, status=result.status, updated_at=utc_now()))
            self._emit("task.finished", actor="horizon", task_id=task.id, status=result.status)
            if session is not None:
                session.write_meta({
                    "role": "horizon", "task": task.id, "status": result.status,
                    "started_at": started.isoformat(), "ended_at": utc_now().isoformat(),
                    "usage": result.metadata.get("usage"),
                    "project_commits": project_commits,
                    "project_commit_errors": project_commit_errors,
                })
            self._integrate_session(
                run,
                session,
                role="horizon",
                project=task.project,
                task_id=task.id,
                project_commits=project_commits,
                project_commit_errors=project_commit_errors,
            )

            boundary = (
                SyncBoundary.ON_BUILD_FAILURE
                if result.status is TaskStatus.FAILED
                else SyncBoundary.AFTER_HORIZON
            )
            self.sync.sync(boundary)

            after = self._session(runlog, f"informal-after-{task.id}")
            followup = self.informal.handle_horizon_result(
                self._informal_context(run, self._log_dir(after)), result
            )
            if after is not None:
                after.write_meta({"role": "informal", "after_task": task.id})
            self._apply_informal(followup, report_name=f"informal-after-{task.id}")
            self._integrate_session(run, after, role="informal-after", project=task.project, task_id=task.id)
            return True
        finally:
            self.locks.release(task.id)

    # ── one round / full run ────────────────────────────────────────

    def run_round(
        self, run: RunRecord, index: int, runlog: RunLog | None = None, *, dry_run: bool = False
    ) -> RoundReport:
        self.sync.sync(SyncBoundary.BEFORE_INFORMAL)
        if self._agent_frozen("informal"):
            self._emit("agent.frozen", agent="informal", round=index)
        else:
            session = self._session(runlog, f"round{index}-informal")
            update = self.informal.run_round(self._informal_context(run, self._log_dir(session)))
            if session is not None:
                session.write_meta({"role": "informal", "round": index, "tasks": len(update.tasks)})
            self._apply_informal(update, report_name=f"round-{index}-informal")
            self._dispatch_subagents(session)
            self._integrate_session(run, session, role="informal")
        self.sync.sync(SyncBoundary.AFTER_INFORMAL)

        selected = self.scheduler.select_tasks(
            self.workspace, run, run.focus, self.task_store.list()
        )

        if dry_run:
            self._emit("run.dry-run", planned=[t.id for t in selected])
            return RoundReport(round_index=index, planned=tuple(t.id for t in selected))

        ran: list[str] = []
        blocked: list[str] = []
        for task in selected:
            (ran if self._run_task(run, task, runlog) else blocked).append(task.id)

        self.sync.sync(SyncBoundary.BEFORE_PUBLISH)
        self.publish(run)
        publish_session = self._session(runlog, f"round{index}-publish")
        if publish_session is not None:
            publish_session.write_meta({"role": "publish", "round": index})
        self._integrate_session(run, publish_session, role="publish")
        return RoundReport(round_index=index, tasks_run=tuple(ran), tasks_blocked=tuple(blocked))

    def run(self, run: RunRecord, *, dry_run: bool = False) -> list[RoundReport]:
        runlog: RunLog | None = None
        if self.run_logs is not None:
            runlog = self.run_logs.get(run.id) if run.id else self.run_logs.allocate()
            run = dataclasses.replace(run, id=runlog.id)
        if self.run_store is not None:
            run = self.run_store.put(run)
        if run.focus.proposal:
            self._execute_proposal(run.focus.proposal)
        self._emit("run.started", run_id=run.id, rounds=run.rounds_requested, dry_run=dry_run)
        reports = [self.run_round(run, i, runlog, dry_run=dry_run) for i in range(run.rounds_requested)]
        self._emit("run.finished", run_id=run.id)
        return reports

    def publish(self, run: RunRecord) -> None:
        """Refresh human-facing artifacts after a collaboration boundary.

        The live dashboard reads stores directly, but a first working version still
        needs durable files a human can inspect without a server: regenerated
        roadmap markdown and per-project blueprint DAG JSON. GitHub publishing is
        intentionally not implicit; the GitHub inbox remains a shadow provider and
        explicit ``gh`` operations live on that provider.
        """
        reports: list[str] = []
        if self.report_store is not None:
            ref = self.report_store.write("roadmap", render_roadmap_markdown(self.roadmap_store.load()))
            reports.append(ref)

        blueprint_refs: list[str] = []
        dags = workspace_dags(self.workspace)
        if dags:
            out_dir = self.workspace.state_path / "blueprints"
            out_dir.mkdir(parents=True, exist_ok=True)
            for project, dag in dags.items():
                path = out_dir / f"{project}.json"
                path.write_text(json.dumps(dag, indent=2, sort_keys=True), "utf-8")
                blueprint_refs.append(path.relative_to(self.workspace.root).as_posix())

        self._emit(
            "publish.completed",
            run_id=run.id,
            reports=reports,
            blueprints=blueprint_refs,
        )
