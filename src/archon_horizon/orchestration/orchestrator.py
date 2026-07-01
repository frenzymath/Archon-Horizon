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

from archon_horizon.agents.base import HorizonAgent, HorizonContext, GroundAgent, GroundContext, GroundUpdate
from archon_horizon.blueprint.checks import blueprint_lint_issues, dag_consistency_issues
from archon_horizon.blueprint.workspace import workspace_dags, workspace_dags_rich
from archon_horizon.core.clock import utc_now
from archon_horizon.core.events import Event
from archon_horizon.core.freeze import FreezeSet, frozen_violations
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxStatus, reaches_horizon
from archon_horizon.core.labels import is_agent_ready
from archon_horizon.core.permissions import WriteDomain, horizon_write_domain, ground_write_domain
from archon_horizon.core.roadmap import Roadmap, RoadmapStatus
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.sessions import Focus, RunRecord, SyncBoundary
from archon_horizon.core.tasks import HorizonResult, HorizonTask, TaskStatus, WriteSet
from archon_horizon.core.workspace import Workspace
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.runlog import RunLog, RunLogTree, SessionLog
from archon_horizon.subagents.base import Subagent, SubagentContext
from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.sink import JsonlTranscriptSink
from archon_horizon.store.base import (
    EventLog,
    MemoryStore,
    RoadmapStore,
    RunStore,
    TaskStore,
)
from archon_horizon.vcs.integration import (
    author_for,
    integrate_workspace_run,
    integrate_workspace_session,
    project_checkpoint,
)

from .locks import LockManager, workspace_run_lock
from .scheduler import Scheduler
from .sync import SyncCoordinator


def _csv(values: object, *, none: str = "none") -> str:
    if isinstance(values, dict):
        values = values.keys()
    if isinstance(values, (list, tuple, set, frozenset)):
        out = ", ".join(str(v) for v in values if v is not None and str(v))
        return out or none
    return str(values) if values not in (None, "") else none


def _short_sha(value: object) -> str:
    text = str(value or "")
    return text[:7] if text else "none"


def _event_label(event_type: str) -> str:
    return event_type.replace(".", " ").replace("_", " ").capitalize()


def _event_summary(event_type: str, data: dict[str, object]) -> str:
    """Human-facing one-line rendering for deterministic system events."""
    if event_type == "run.started":
        rounds = data.get("rounds")
        suffix = "round" if rounds == 1 else "rounds"
        return f"Started run {data.get('run_id')} ({rounds} {suffix})."
    if event_type == "run.finished":
        return f"Finished run {data.get('run_id')}."
    if event_type == "run.stopped":
        return f"Stopped run {data.get('run_id')}: {data.get('reason')}."
    if event_type == "run.focus_broadened":
        return (
            f"Focus {_csv(data.get('focus_tasks'))} had no runnable task; ran the active roadmap work "
            f"in project(s) {_csv(data.get('projects'))}: {_csv(data.get('selected'))}."
        )
    if event_type == "run.dry-run":
        return f"Dry run planned tasks: {_csv(data.get('planned'))}."
    if event_type == "roadmap.load_failed":
        return f"Roadmap load failed: {data.get('error')}."
    if event_type == "roadmap.restored":
        return "Restored roadmap from the last clean in-memory copy."
    if event_type == "roadmap.updated":
        return "Roadmap reloaded after Ground session."
    if event_type == "report.written":
        return f"Report saved: {data.get('ref') or data.get('name')}."
    if event_type == "blueprint.checks.findings":
        count = data.get("count")
        projects = data.get("projects")
        if isinstance(projects, dict) and projects:
            detail = ", ".join(f"{project}: {n}" for project, n in projects.items())
            return f"Blueprint checks found {count} issue(s): {detail}."
        return f"Blueprint checks found {count} issue(s)."
    if event_type == "blueprint.checks.clean":
        return "Blueprint checks found no issues."
    if event_type == "task.created":
        return f"Created task {data.get('task_id')} for project {data.get('project')}."
    if event_type == "task.started":
        return f"Started Horizon task {data.get('task_id')}."
    if event_type == "task.finished":
        return f"Finished Horizon task {data.get('task_id')} with status: {data.get('status')}."
    if event_type == "task.blocked":
        return f"Blocked task {data.get('task_id')}: {data.get('reason')}."
    if event_type == "task.deferred":
        return f"Deferred task {data.get('task_id')}: {data.get('reason')}."
    if event_type == "agent.frozen":
        return f"Skipped {data.get('agent')} because that agent is frozen."
    if event_type == "write_domain.violation":
        return f"Write-domain violation for task {data.get('task_id')}: {_csv(data.get('paths'))}."
    if event_type == "project.commit":
        project = data.get("project")
        if data.get("changed"):
            return f"Committed {project} changes: {_short_sha(data.get('sha'))}."
        return f"No project changes to commit for {project}."
    if event_type == "project.commit.failed":
        return f"Project checkpoint failed for {data.get('project')}: {data.get('error')}."
    if event_type == "workspace.session.integrated":
        role = data.get("role")
        session = data.get("session")
        if data.get("workspace_commit_error"):
            return f"Integration of {role} session {session} failed: {data.get('workspace_commit_error')}."
        sha = data.get("workspace_commit")
        if sha:
            return f"Integrated {role} session {session}: {_short_sha(sha)}."
        return f"Integrated {role} session {session}; no workspace changes."
    if event_type == "run.concurrent":
        existing = data.get("existing") or {}
        return (
            f"Another horizon run is active on this workspace "
            f"(pid {existing.get('pid')} on {existing.get('host')}, run {existing.get('run_id') or '?'}); "
            "proceeding alongside it — shared roadmap/blueprint writes may interleave."
        )
    if event_type == "workspace.run_integrated":
        if data.get("changed"):
            return f"Integrated run {data.get('run_id')} into workspace: {_short_sha(data.get('sha'))}."
        return f"No workspace changes to integrate for run {data.get('run_id')}."
    if event_type == "workspace.run_integrate.failed":
        return f"Workspace run integration failed: {data.get('error')}."
    if event_type == "subagent.ran":
        return (
            f"Subagent {data.get('name')} finished "
            f"(ok={data.get('ok')}, issues={data.get('issues_reported', 0)})."
        )
    if event_type == "publish.completed":
        blueprints = data.get("blueprints")
        n = len(blueprints) if isinstance(blueprints, (list, tuple)) else 0
        return f"Published deterministic artifacts ({n} blueprint DAG(s))."
    return _event_label(event_type) + "."


def _checklist_item(event: TranscriptEvent) -> str:
    data = event.data
    event_type = str(event.tool or "")
    summary = event.text.strip() or _event_summary(event_type, data)
    checked = event.kind is not TranscriptKind.ERROR and not _is_issue_event(event)
    return f"- [{'x' if checked else ' '}] {summary}"


def _is_issue_event(event: TranscriptEvent) -> bool:
    event_type = str(event.tool or "")
    data = event.data
    if event.kind is TranscriptKind.ERROR:
        return True
    if "failed" in event_type or "violation" in event_type:
        return True
    if event_type in {
        "blueprint.checks.findings",
        "task.blocked",
        "task.deferred",
        "roadmap.load_failed",
        "agent.frozen",
        "run.stopped",
    }:
        return True
    status = str(data.get("status") or "")
    if status in {TaskStatus.FAILED.value, TaskStatus.BLOCKED.value, "failed", "blocked"}:
        return True
    exit_code = data.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


@dataclass(frozen=True, slots=True)
class RoundReport:
    round_index: int
    tasks_run: tuple[str, ...] = ()
    tasks_blocked: tuple[str, ...] = ()
    planned: tuple[str, ...] = ()


@dataclass(slots=True)
class Orchestrator:
    workspace: Workspace
    ground: GroundAgent
    horizon: HorizonAgent
    scheduler: Scheduler
    sync: SyncCoordinator
    locks: LockManager
    event_log: EventLog
    roadmap_store: RoadmapStore
    memory_store: MemoryStore
    task_store: TaskStore
    inbox_providers: Sequence[InboxProvider] = ()
    run_store: RunStore | None = None
    run_logs: RunLogTree | None = None
    ground_subagents: tuple[Subagent, ...] = ()
    freeze: FreezeSet = field(default_factory=FreezeSet)
    # The run is a flat ground/horizon alternation that opens and closes on
    # ground by default; set either to "horizon" to skip the opening / closing
    # ground so the run starts and/or ends on horizon instead.
    start_with: str = "ground"
    end_with: str = "ground"
    # Last roadmap that parsed cleanly; kept in memory if a human-readable item
    # shard is malformed, so one bad edit can't crash the whole run.
    _roadmap_cache: Roadmap | None = field(default=None, repr=False)
    # Orchestrator events buffered into a "system" session transcript, so the
    # deterministic work between agents (commits, sync, integration, publish) is
    # visible in the Log alongside the ground/horizon sessions.
    _system_buffer: list[TranscriptEvent] = field(default_factory=list, repr=False)
    _collecting: bool = field(default=False, repr=False)
    # The system session currently being written. Consecutive deterministic work
    # (with no agent session in between) appends to it, so the Log shows ONE
    # system session per boundary rather than several tiny ones. Finalized (its
    # session_end + report written) when the next agent runs or the run ends.
    _open_system: "SessionLog | None" = field(default=None, repr=False)
    _open_system_events: list[TranscriptEvent] = field(default_factory=list, repr=False)

    # ── event helper ────────────────────────────────────────────────

    def _emit(self, type: str, actor: str = "orchestrator", **data: object) -> None:
        self.event_log.append(Event(type=type, id=uuid.uuid4().hex, actor=actor, data=data))
        if self._collecting:
            is_error = "fail" in type or "error" in type
            event_data = {"actor": actor, "type": type, **data}
            self._system_buffer.append(TranscriptEvent(
                TranscriptKind.ERROR if is_error else TranscriptKind.TOOL_CALL,
                tool="" if is_error else type,
                text=_event_summary(type, event_data),
                data=event_data,
            ))

        from archon_horizon.log import log
        if type == "run.started":
            log.header(f"Starting Run (ID: {data.get('run_id')}) - {data.get('rounds')} rounds")
        elif type == "run.finished":
            log.success(f"Run {data.get('run_id')} finished.")
        elif type == "task.started":
            log.step(f"[{actor}] Started task: {data.get('task_id')}")
        elif type == "task.finished":
            status_str = str(data.get('status')).split('.')[-1]
            log.step(f"[{actor}] Finished task: {data.get('task_id')} ({status_str})")
        elif type == "subagent.ran":
            log.step(f"[ground] Subagent '{data.get('name')}' finished (ok={data.get('ok')})")
        elif type == "project.commit":
            sha = str(data.get('sha'))[:7] if data.get('sha') else "none"
            log.step(f"Committed changes to {data.get('project')} ({sha})")

    @staticmethod
    def _session(runlog: RunLog | None, label: str) -> SessionLog | None:
        return runlog.new_session(label) if runlog is not None else None

    @staticmethod
    def _log_dir(session: SessionLog | None) -> Path | None:
        return session.path if session is not None else None

    def _revision_meta(self) -> dict[str, object]:
        from archon_horizon.vcs.git import WorkspaceGit, collect_revisions, git_available

        if not git_available():
            return {}
        workspace_sha = WorkspaceGit(self.workspace.root).current_sha()
        project_revisions = {k: v for k, v in collect_revisions(self.workspace).items() if v}
        out: dict[str, object] = {}
        if workspace_sha:
            out["workspace_sha"] = workspace_sha
        if project_revisions:
            out["project_revisions"] = project_revisions
        return out

    def _write_session_meta(self, session: SessionLog | None, meta: dict[str, object]) -> None:
        if session is None:
            return
        session.write_meta({**meta, **self._revision_meta()})

    def _integrate_session(
        self,
        run: RunRecord,
        session: SessionLog | None,
        *,
        role: str,
        round_index: int | None = None,
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
            round_index=round_index,
            project=project,
            task_id=task_id,
            project_commits=project_commits,
            project_commit_errors=project_commit_errors,
            commit_workspace=True,
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
        )

    # ── context assembly ────────────────────────────────────────────

    def _accepted_inbox(self) -> tuple[InboxItem, ...]:
        items: list[InboxItem] = []
        for provider in self.inbox_providers:
            for item in provider.list_items():
                if item.status is InboxStatus.OPEN and is_agent_ready(item.labels):
                    items.append(item)
        return tuple(items)

    @staticmethod
    def _render_memory(items: tuple[InboxItem, ...]) -> str:
        """Memory now lives in the inbox: render the open MEMORY items as text."""
        notes = [i for i in items if i.kind is InboxKind.MEMORY]
        return "\n".join(f"- {i.body.strip()}" for i in notes)

    def _load_roadmap(self) -> Roadmap:
        """Load the sharded roadmap, tolerating a malformed item shard.

        Ground should mutate roadmap items through the CLI/store, but the
        on-disk shards are still human-readable YAML. If one is malformed, fall
        back to the last clean in-memory roadmap so a run can continue.

        The fallback is *non-destructive*: it never writes back over the on-disk
        shards. An earlier version re-saved the cached roadmap here, but
        ``save`` deletes any item shard not in the set it is given — so a single
        malformed shard would wipe out every other item added to disk since the
        cache was taken. We leave the shards untouched; the bad one is repaired
        by whoever wrote it, and the next clean load picks up everything."""
        try:
            roadmap = self.roadmap_store.load()
        except Exception as exc:
            self._emit("roadmap.load_failed", actor="orchestrator", error=str(exc))
            if self._roadmap_cache is not None:
                self._emit("roadmap.restored", actor="orchestrator")
                return self._roadmap_cache
            return Roadmap()
        self._roadmap_cache = roadmap
        return roadmap

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

    def _active_projects(self, run: RunRecord) -> tuple[str, ...]:
        if run.focus.projects:
            return run.focus.projects
        if run.focus.tasks:
            tasks = {task.id: task for task in self.task_store.list()}
            projects: list[str] = []
            for task_id in run.focus.tasks:
                task = tasks.get(task_id)
                if task is None:
                    continue
                projects.extend(task.projects or ((task.project,) if task.project else ()))
            if projects:
                return tuple(dict.fromkeys(projects))
        return tuple(self.workspace.projects)

    def _ground_context(
        self, run: RunRecord, log_dir: Path | None = None, resume_session_id: str | None = None
    ) -> GroundContext:
        accepted = self._accepted_inbox()
        return GroundContext(
            workspace=self.workspace,
            run=run,
            focus=run.focus,
            roadmap=self._load_roadmap(),
            accepted_inbox=accepted,
            memory=self._render_memory(accepted),
            blueprint_summary=self._blueprint_summary(),
            write_domain=ground_write_domain(self.workspace, self._active_projects(run)).allow,
            log_dir=log_dir,
            resume_session_id=resume_session_id,
        )

    # ── freeze / subagents ──────────────────────────────────────────

    def _agent_frozen(self, agent: str) -> bool:
        return self.freeze.agent_rule(agent) is not None

    def _local_provider(self) -> InboxProvider | None:
        for provider in self.inbox_providers:
            if "create" in provider.capabilities:
                return provider
        return None

    def _dispatch_subagents(self, parent: SessionLog | None) -> None:
        for sub in self.ground_subagents:
            session = parent.new_subsession(sub.name) if parent is not None else None
            result = sub.run(SubagentContext(self.workspace, self._log_dir(session)))
            if session is not None:
                if not session.transcript_path.exists():
                    sink = JsonlTranscriptSink(session.transcript_path)
                    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"subagent": sub.name}))
                    if result.report:
                        sink.emit(TranscriptEvent(TranscriptKind.TEXT, text=result.report))
                    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": result.ok}))
                self._write_session_meta(session, {
                    "role": "subagent",
                    "name": sub.name,
                    "ok": result.ok,
                    "data": result.data,
                })
            self._emit("subagent.ran", name=sub.name, ok=result.ok, issues_reported=len(result.issues))

    def _run_blueprint_checks(self) -> None:
        """Run deterministic blueprint checks without creating inbox items.

        The inbox is authored explicitly by humans/Ground/Horizon through
        ``horizon inbox``. Deterministic audits are telemetry only, otherwise a
        large workspace can flood the shared inbox with machine-generated items.
        """
        total = 0
        by_project: dict[str, int] = {}
        for project, dag in workspace_dags(self.workspace).items():
            try:
                count = len(dag_consistency_issues(dag) + blueprint_lint_issues(dag))
            except Exception as exc:  # a pathological DAG must not abort the run
                self._emit("blueprint.checks.failed", actor="orchestrator", project=project, error=str(exc))
                continue
            if count:
                by_project[project] = count
                total += count
        if total:
            self._emit(
                "blueprint.checks.findings",
                actor="orchestrator",
                count=total,
                projects=by_project,
            )
        else:
            self._emit("blueprint.checks.clean", actor="orchestrator")

    def _task_projects(self, task: HorizonTask) -> tuple[str, ...]:
        """The full project set a task covers — a task may span several projects.

        Falls back to the single ``project`` field, then to the whole workspace
        for a project-less (workspace-wide) task.
        """
        projects = task.projects or ((task.project,) if task.project else ())
        return projects or tuple(self.workspace.projects)

    def _horizon_context(
        self,
        run: RunRecord,
        task: HorizonTask,
        domain: WriteDomain,
        runlog: RunLog | None,
        log_dir: Path | None = None,
        resume_session_id: str | None = None,
    ) -> HorizonContext:
        roadmap = self._load_roadmap()
        projects = self._task_projects(task)
        accepted = tuple(
            i for i in self._accepted_inbox()
            if any(reaches_horizon(i, p) for p in projects)
        )
        return HorizonContext(
            workspace=self.workspace,
            run=run,
            task=task,
            roadmap=roadmap.slice_for_projects(set(projects)),
            accepted_inbox=accepted,
            memory=self._render_memory(accepted),
            write_domain=domain.allow,
            log_dir=log_dir,
            resume_session_id=resume_session_id,
            metadata={"ground_recommendation": self._latest_ground_recommendation(runlog)},
        )

    def _dirty_files(self) -> frozenset[str]:
        """Workspace-relative paths dirty in the working tree right now (or empty
        when git is unavailable). Used to snapshot pre-existing changes so the
        write-domain check attributes only what a session newly touched."""
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        if not git_available():
            return frozenset()
        return frozenset(WorkspaceGit(self.workspace.root).changed_files())

    def _enforce_write_domain(
        self, task: HorizonTask, domain: WriteDomain, before: frozenset[str] = frozenset()
    ) -> None:
        """Flag (don't revert) files the free Horizon agent wrote outside its lane.

        Only files the session NEWLY changed are considered — ``before`` is the
        set of paths already dirty when the session started, so pre-existing
        working-tree changes (manual edits, prior sessions) are never
        mis-attributed. Violations are logged as events for Ground/humans to inspect.
        """
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        if not git_available():
            return
        # The state dir is orchestrator-owned (events, runs, run-local reports);
        # never attribute those writes to the agent.
        state_prefix = self.workspace.state_dir.as_posix() + "/"
        changed = tuple(
            p for p in WorkspaceGit(self.workspace.root).changed_files()
            if not p.startswith(state_prefix) and p not in before
        )
        violations = domain.violations(changed)
        if not violations:
            return
        self._emit(
            "write_domain.violation",
            actor="orchestrator",
            task_id=task.id,
            paths=list(violations),
        )


    # ── applying a Ground update ────────────────────────────────────

    @staticmethod
    def _write_run_report(session: SessionLog | None, text: str) -> str | None:
        if session is None or not text:
            return None
        path = session.path / "report.md"
        path.write_text(text.rstrip() + "\n", "utf-8")
        return path.as_posix()

    @staticmethod
    def _extract_recommendation(text: str) -> str:
        """Return the recommendation section Ground left for the next Horizon.

        Ground reports are intentionally prose, so this is a small Markdown
        convention rather than a parser contract: prefer an explicit
        recommendation/next section, otherwise keep the whole report.
        """
        lines = text.strip().splitlines()
        if not lines:
            return ""
        section_starts = [
            idx for idx, line in enumerate(lines)
            if line.lstrip("# ").strip().lower() in {
                "next",
                "recommendation",
                "recommendations",
                "recommended focus",
                "horizon recommendation",
            }
        ]
        if not section_starts:
            return text.strip()
        start = section_starts[-1]
        start_line = lines[start].lstrip()
        level = len(start_line) - len(start_line.lstrip("#"))
        end = len(lines)
        for idx in range(start + 1, len(lines)):
            line = lines[idx].lstrip()
            if line.startswith("#"):
                idx_level = len(line) - len(line.lstrip("#"))
                if idx_level <= level:
                    end = idx
                    break
        return "\n".join(lines[start:end]).strip()

    @classmethod
    def _write_recommendation(cls, session: SessionLog | None, text: str) -> str | None:
        recommendation = cls._extract_recommendation(text)
        if session is None or not recommendation:
            return None
        path = session.path / "recommendation.md"
        path.write_text(recommendation.rstrip() + "\n", "utf-8")
        return path.as_posix()

    @staticmethod
    def _latest_ground_recommendation(runlog: RunLog | None) -> str:
        if runlog is None:
            return ""
        for session in reversed(runlog.sessions()):
            meta = session.read_meta()
            if meta.get("role") != "ground" and not session.name.endswith("-ground"):
                continue
            path = session.path / "recommendation.md"
            if path.exists():
                return path.read_text("utf-8").strip()
        return ""

    def _after_ground(
        self,
        update: GroundUpdate,
        *,
        report_name: str = "ground",
        session: SessionLog | None = None,
    ) -> None:
        """Persist the run-local report and reconcile state the agent wrote to disk.

        Ground mutates roadmap and inbox state through the CLI during its run,
        so we reload from disk rather than apply a parsed payload. The dashboard
        reads the stores directly, so no markdown artifact is rendered.
        """
        ref = self._write_run_report(session, update.report)
        if ref is not None:
            self._emit("report.written", name=report_name, ref=ref)
        recommendation_ref = self._write_recommendation(session, update.report)
        if recommendation_ref is not None:
            self._emit("report.written", name=f"{report_name}-recommendation", ref=recommendation_ref)
        self._emit("roadmap.updated", actor="ground")

    def _focus_fallback(self, run: RunRecord, candidates: list[HorizonTask]) -> Focus:
        """A project-scoped focus derived from the originally targeted task(s).

        ``horizon run <X>`` pins the focus to task ``X``; but the Ground agent
        plans by marking roadmap items ACTIVE, and the orchestrator queues those
        as tasks under the ITEM ids — never ``X``. So once ``X`` itself is done
        (or is just a planning seed), the focus matches nothing runnable. Falling
        back to ``X``'s project(s) lets the scheduler dispatch the roadmap work
        Ground created in the same project instead of stopping the run."""
        by_id = {task.id: task for task in candidates}
        projects: list[str] = []
        for task_id in run.focus.tasks:
            task = by_id.get(task_id)
            if task is None:
                try:
                    task = self.task_store.get(task_id)
                except Exception:
                    task = None
            if task is not None:
                projects.extend(task.projects or ((task.project,) if task.project else ()))
        return dataclasses.replace(
            run.focus, tasks=(), task=None, projects=tuple(dict.fromkeys(p for p in projects if p))
        )

    def _sync_roadmap_tasks(self, run: RunRecord) -> None:
        """Derive Horizon work from active roadmap items.

        The Ground agent recommends work by marking a roadmap item ACTIVE; the
        orchestrator turns each unblocked active item into a queued Horizon
        task. Task ids mirror the item id so this is idempotent across rounds;
        a finished task is re-queued only if its item is re-activated by the
        Ground agent.
        """
        roadmap = self._load_roadmap()
        existing = {t.id: t for t in self.task_store.list()}
        items_by_id = {item.id: item for item in roadmap.items}
        for item in roadmap.items:
            if item.status is not RoadmapStatus.ACTIVE or not item.projects:
                continue
            # Task status (DONE/FAILED/BLOCKED) is user-facing metadata, never a
            # scheduling gate: an ACTIVE roadmap item is re-derived and re-run
            # every round — a long item makes more progress each round even
            # though each Horizon pass returns "done". Ground retires an item by
            # marking it DONE *in the roadmap*, not via the task's status. (So a
            # resume / new run picks an active item back up even if its task
            # finished in a prior run — that was the run-0008 stall.)
            prior = existing.get(item.id)
            unmet = tuple(
                dep
                for dep in item.depends_on
                if (dep not in items_by_id) or items_by_id[dep].status is not RoadmapStatus.DONE
            )
            objective = item.title if not item.summary else f"{item.title}\n\n{item.summary}"
            metadata = {**item.metadata, "roadmap_priority": item.priority}
            if unmet:
                self.task_store.put(
                    HorizonTask(
                        id=item.id,
                        project=item.projects[0],
                        objective=objective,
                        title=item.title,
                        explanation=item.summary,
                        projects=item.projects,
                        priority=item.priority,
                        status=TaskStatus.BLOCKED,
                        write_set=self._roadmap_item_write_set(item),
                        scope=item.scope,
                        roadmap_refs=(item.id,),
                        inbox_refs=item.inbox_refs,
                        metadata={**metadata, "blocked_by_roadmap": unmet},
                    )
                )
                if prior is None or prior.status is not TaskStatus.BLOCKED:
                    self._emit(
                        "task.blocked",
                        actor="orchestrator",
                        task_id=item.id,
                        reason="roadmap-dependency",
                        dependencies=unmet,
                    )
                continue
            self.task_store.put(
                HorizonTask(
                    id=item.id,
                    project=item.projects[0],
                    objective=objective,
                    title=item.title,
                    explanation=item.summary,
                    projects=item.projects,
                    priority=item.priority,
                    status=TaskStatus.QUEUED,
                    write_set=self._roadmap_item_write_set(item),
                    scope=item.scope,
                    roadmap_refs=(item.id,),
                    inbox_refs=item.inbox_refs,
                    metadata=metadata,
                )
            )
            if prior is None:
                self._emit("task.created", actor="orchestrator", task_id=item.id, project=item.projects[0])

    @staticmethod
    def _roadmap_item_write_set(item: object) -> WriteSet:
        metadata = getattr(item, "metadata", {}) or {}
        raw = metadata.get("write_set") if isinstance(metadata, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        scope = getattr(item, "scope", ItemScope())
        projects = raw.get("projects", scope.targets("projects", writable_only=True) or getattr(item, "projects", ()))
        return WriteSet(
            files=tuple(raw.get("files", scope.targets("files", writable_only=True))),
            projects=tuple(projects or ()),
            declarations=tuple(raw.get("declarations", scope.targets("declarations", writable_only=True))),
            blueprint_nodes=tuple(raw.get("blueprint_nodes", raw.get("blueprint-nodes", scope.targets("blueprint_nodes", writable_only=True)))),
            workspace=bool(raw.get("workspace", False)),
        )

    # ── one Ground / one Horizon step ───────────────────────────────

    @staticmethod
    def _project_commit_message(
        run: RunRecord, task: HorizonTask, session: SessionLog | None, project: str, round_index: int
    ) -> str:
        """A project checkpoint message whose subject encodes the step
        (run / round / task / project) and carries a human summary, mirroring the
        ``role[run round task](project): summary`` house style."""
        summary = (task.title or task.objective or "").strip().splitlines()[0] if (task.title or task.objective) else ""
        summary = summary[:100]
        subject = f"horizon[{run.id} r{round_index} {task.id}]({project}): {summary}".rstrip(": ")
        body = [
            f"Run: {run.id}",
            f"Round: {round_index}",
            "Role: horizon",
            f"Session: {session.name if session else '—'}",
            f"Task: {task.id}",
            f"Project: {project}",
        ]
        return subject + "\n\n" + "\n".join(body)

    def _checkpoint_projects(
        self, run: RunRecord, task: HorizonTask, session: SessionLog | None, *, round_index: int
    ) -> tuple[dict[str, str | None], dict[str, str]]:
        """Commit a checkpoint in *every* project the task spans (a task may touch
        several), so no project's work is left uncommitted."""
        project_commits: dict[str, str | None] = {}
        project_commit_errors: dict[str, str] = {}
        for proj in self._task_projects(task):
            checkpoint = project_checkpoint(
                self.workspace,
                proj,
                message=self._project_commit_message(run, task, session, proj, round_index),
                author=author_for("horizon"),
            )
            if checkpoint.error:
                project_commit_errors[proj] = checkpoint.error
                self._emit("project.commit.failed", project=proj, task_id=task.id, error=checkpoint.error)
            elif checkpoint.attempted:
                project_commits[proj] = checkpoint.sha
                self._emit(
                    "project.commit",
                    project=proj,
                    task_id=task.id,
                    sha=checkpoint.sha,
                    changed=checkpoint.changed,
                )
        return project_commits, project_commit_errors

    def _ground_step(
        self,
        run: RunRecord,
        runlog: RunLog | None,
        *,
        round_index: int,
        horizon_result: HorizonResult | None = None,
        resume_session_id: str | None = None,
    ) -> None:
        """One Ground session in the flat alternation. With no ``horizon_result``
        it is the opening plan (``run_round``); otherwise it reconciles the
        Horizon step that just ran (``handle_horizon_result``)."""
        self._finalize_system_session()  # close any open system session before an agent runs
        self.sync.sync(SyncBoundary.BEFORE_GROUND)
        if self._agent_frozen("ground"):
            self._emit("agent.frozen", agent="ground", round=round_index)
            self.sync.sync(SyncBoundary.AFTER_GROUND)
            return
        session = self._session(runlog, "ground")
        context = self._ground_context(run, self._log_dir(session), resume_session_id)
        if horizon_result is None:
            update = self.ground.run_round(context)
        else:
            update = self.ground.handle_horizon_result(context, horizon_result)
        # Reload now so malformed human-readable roadmap shards are noticed
        # immediately and the cached clean roadmap remains available.
        self._load_roadmap()
        if session is not None:
            meta: dict[str, object] = {"role": "ground", "round": round_index}
            session_id = update.metadata.get("session_id")
            if session_id:
                meta["engine_session_id"] = session_id
            for key in ("harness_name", "harness_kind", "model", "usage"):
                value = update.metadata.get(key)
                if value is not None:
                    meta[key] = value
            self._write_session_meta(session, meta)
        self._after_ground(update, report_name=f"ground-{round_index}", session=session)
        self._run_blueprint_checks()
        self._integrate_session(run, session, role="ground", round_index=round_index)
        self.sync.sync(SyncBoundary.AFTER_GROUND)

    def _horizon_step(
        self, run: RunRecord, task: HorizonTask, runlog: RunLog | None, *, round_index: int,
        resume_session_id: str | None = None,
    ) -> HorizonResult | None:
        """One Horizon session: run a single task. Returns its result, or ``None``
        if it could not run (frozen / lock conflict) so the caller skips the
        reconcile pairing for it."""
        self.sync.sync(SyncBoundary.BEFORE_HORIZON)

        if self._agent_frozen("horizon"):
            self.task_store.put(dataclasses.replace(task, status=TaskStatus.BLOCKED, updated_at=utc_now()))
            self._emit("task.blocked", task_id=task.id, reason="agent-freeze")
            return None

        violations = frozen_violations(task.write_set, self.freeze)
        if violations:
            self.task_store.put(dataclasses.replace(task, status=TaskStatus.BLOCKED, updated_at=utc_now()))
            self._emit("task.blocked", task_id=task.id, reason="freeze",
                       rules=[r.pattern for r in violations])
            return None

        if not self.locks.acquire(task.id, task.write_set):
            self._emit("task.deferred", task_id=task.id, reason="lock-conflict")
            return None

        try:
            self.task_store.put(dataclasses.replace(task, status=TaskStatus.RUNNING, updated_at=utc_now()))
            self._finalize_system_session()  # close any open system session before Horizon runs
            self._emit("task.started", actor="horizon", task_id=task.id)

            session = self._session(runlog, f"horizon-{task.id}")
            started = utc_now()
            domain = horizon_write_domain(self.workspace, self._task_projects(task))
            before = self._dirty_files()
            result = self.horizon.run_task(
                self._horizon_context(run, task, domain, runlog, self._log_dir(session), resume_session_id)
            )
            self._enforce_write_domain(task, domain, before)
            project_commits, project_commit_errors = self._checkpoint_projects(
                run, task, session, round_index=round_index
            )
            ref = self._write_run_report(session, result.report)
            if ref is not None:
                self._emit("report.written", name=f"task-{task.id}", ref=ref)
            self.task_store.put(dataclasses.replace(task, status=result.status, updated_at=utc_now()))
            self._emit("task.finished", actor="horizon", task_id=task.id, status=result.status)
            if session is not None:
                self._write_session_meta(session, {
                    "role": "horizon",
                    "round": round_index,
                    "project": task.project,
                    "task_id": task.id,
                    "status": result.status.value,
                    "started_at": started.isoformat(),
                    "ended_at": utc_now().isoformat(),
                    "harness_name": result.metadata.get("harness_name"),
                    "harness_kind": result.metadata.get("harness_kind"),
                    "model": result.metadata.get("model"),
                    "usage": result.metadata.get("usage"),
                    "engine_session_id": result.metadata.get("session_id"),
                    "project_commits": project_commits,
                    "project_commit_errors": project_commit_errors,
                })
            self._integrate_session(
                run,
                session,
                role="horizon",
                round_index=round_index,
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
            return result
        finally:
            self.locks.release(task.id)

    def _publish_silent(self, run: RunRecord) -> None:
        """Refresh human-facing artifacts at a boundary and pull external inboxes,
        emitting the result so the deterministic GitHub/DAG work is attributed to
        the system session rather than happening invisibly."""
        for result in self.sync.sync(SyncBoundary.BEFORE_PUBLISH):
            if result.imported or result.updated or result.skipped or result.errors:
                self._emit(
                    "inbox.synced" if not result.errors else "inbox.sync.failed",
                    provider=result.provider,
                    imported=result.imported,
                    updated=result.updated,
                    skipped=result.skipped,
                    errors=list(result.errors),
                )
        self.publish(run)

    def _flush_system_session(self, runlog: RunLog | None, label: str = "system") -> None:
        """Append the buffered orchestrator events (commits, sync, integration,
        publish since the last flush) to the open ``system`` session — opening one
        if none is open — so the deterministic work between agents shows up in the
        Log as ONE session. ``_finalize_system_session`` closes it (writes its
        ``session_end`` + report) when an agent runs next or the run ends."""
        if not self._system_buffer:
            return
        if self._open_system is None:
            session = self._session(runlog, label)
            if session is None:
                self._system_buffer.clear()
                return
            self._open_system = session
            self._open_system_events = []
            JsonlTranscriptSink(session.transcript_path).emit(
                TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "system"})
            )
        sink = JsonlTranscriptSink(self._open_system.transcript_path)
        for event in self._system_buffer:
            sink.emit(event)
            self._open_system_events.append(event)
        self._system_buffer.clear()

    def _finalize_system_session(self) -> None:
        """Close the open system session: flush any tail events, then write its
        ``session_end`` + report. A no-op when no system session is open."""
        if self._system_buffer and self._open_system is not None:
            sink = JsonlTranscriptSink(self._open_system.transcript_path)
            for event in self._system_buffer:
                sink.emit(event)
                self._open_system_events.append(event)
            self._system_buffer.clear()
        if self._open_system is None:
            return
        JsonlTranscriptSink(self._open_system.transcript_path).emit(
            TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": True})
        )
        self._write_session_meta(self._open_system, {"role": "system"})
        self._write_system_report(self._open_system, self._open_system_events)
        self._open_system = None
        self._open_system_events = []

    @staticmethod
    def _write_system_report(session: SessionLog, events: Sequence[TranscriptEvent]) -> None:
        checklist = [_checklist_item(event) for event in events]
        issues = [
            f"- {event.text.strip() or _event_summary(str(event.tool or ''), event.data)}"
            for event in events
            if _is_issue_event(event)
        ]
        body = (
            "## Checklist\n\n"
            + ("\n".join(checklist) if checklist else "- [x] No deterministic system work recorded.")
            + "\n\n## Issues\n\n"
            + ("\n".join(issues) if issues else "- None.")
            + "\n"
        )
        (session.path / "report.md").write_text(body, "utf-8")

    # ── resume ───────────────────────────────────────────────────────

    @staticmethod
    def _session_complete(session: SessionLog) -> bool:
        """A session finished iff its transcript carries a ``session_end`` — the
        harness writes that last, so its absence means the process was killed
        mid-session (the interruption a resume must re-run)."""
        from archon_horizon.transcript.sink import read_transcript

        if not session.transcript_path.exists():
            return False
        try:
            events = read_transcript(session.transcript_path)
        except Exception:
            return False
        return any(e.kind is TranscriptKind.SESSION_END for e in events)

    def _resume_point(self, runlog: RunLog) -> tuple[bool, int, bool]:
        """Where the interrupted run should pick back up, read from on-disk sessions.

        The flat layout is ``[ground, horizon_0, ground_1, horizon_1, …]`` — index
        0 is the opening Ground, then each round ``i`` owns sessions ``2i+1`` (its
        Horizon) and ``2i+2`` (its reconcile Ground). Returns ``(run_opening,
        resume_round, reconcile_only)``: redo the opening Ground, the first round
        to (re)run, and whether only that round's reconcile Ground remains (its
        Horizon already finished, so just re-run the Ground with the recovered
        Horizon result).
        """
        sessions = [s for s in runlog.sessions() if not s.name.endswith("-system")]
        if not sessions or not self._session_complete(sessions[0]):
            return True, 0, False  # opening never finished → redo from the top
        t = 1
        while t < len(sessions) and self._session_complete(sessions[t]):
            t += 1
        # ``t`` is the first unfinished session, or the next to create when all
        # present ones finished cleanly (the run stopped between sessions).
        round_index = (t - 1) // 2
        reconcile_only = (t - 1) % 2 == 1
        return False, round_index, reconcile_only

    def _recover_engine_session_id(self, runlog: RunLog, session_index: int) -> str | None:
        """The native engine session id of an interrupted session, for a native
        ``--resume``. Prefer the session ``meta.json`` (written on a clean exit),
        then the transcript's ``session_end``/``session_meta`` (so a session that
        crashed mid-run is still resumable from its partial stream)."""
        from archon_horizon.transcript.sink import read_transcript

        sessions = [s for s in runlog.sessions() if not s.name.endswith("-system")]
        if session_index < 0 or session_index >= len(sessions):
            return None
        session = sessions[session_index]
        sid = session.read_meta().get("engine_session_id")
        if isinstance(sid, str) and sid:
            return sid
        if not session.transcript_path.exists():
            return None
        try:
            events = read_transcript(session.transcript_path)
        except Exception:
            return None
        for kind in (TranscriptKind.SESSION_END, TranscriptKind.SESSION_META):
            for event in reversed(events):
                if event.kind is kind:
                    candidate = event.data.get("session_id")
                    if isinstance(candidate, str) and candidate:
                        return candidate
        return None

    def _recover_horizon_result(self, runlog: RunLog, round_index: int) -> HorizonResult | None:
        """Rebuild a finished Horizon step's result from its session, so a resume
        that only needs the reconcile Ground can feed it the prior Horizon output."""
        # Index against the agent-session layout only — system sessions are
        # interleaved on disk but excluded from the 2i+1/2i+2 scheme (matching
        # ``_resume_point`` and ``_recover_engine_session_id``).
        sessions = [s for s in runlog.sessions() if not s.name.endswith("-system")]
        idx = 2 * round_index + 1
        if idx >= len(sessions):
            return None
        session = sessions[idx]
        meta = session.read_meta()
        report_path = session.path / "report.md"
        report = report_path.read_text("utf-8") if report_path.exists() else ""
        raw_status = meta.get("status")
        status = TaskStatus(raw_status) if raw_status else TaskStatus.DONE
        return HorizonResult(task_id=str(meta.get("task_id", "")), status=status, report=report)

    # ── full run ─────────────────────────────────────────────────────

    def _run_scope_projects(self, run: RunRecord) -> tuple[str, ...]:
        projects: list[str] = list(run.focus.projects)
        task_ids = list(run.focus.tasks)
        if run.focus.task:
            task_ids.append(run.focus.task)
        for task_id in task_ids:
            try:
                task = self.task_store.get(task_id)
            except Exception:
                continue
            projects.extend(self._task_projects(task))
        if not projects:
            projects.extend(self.workspace.projects)
        return tuple(dict.fromkeys(projects))

    def run(self, run: RunRecord, *, dry_run: bool = False, resume: bool = False) -> list[RoundReport]:
        """Drive a flat ground/horizon alternation: G H G H … G.

        One Ground opens the run (plan), then each round runs one Horizon step
        followed by a reconcile Ground, so the sequence is a clean single-G/single-H
        alternation that opens and closes on Ground. ``start_with``/``end_with`` =
        ``"horizon"`` drop the opening / final Ground. Publish is silent
        bookkeeping between steps, not a session.

        With ``resume`` the run id must already exist: finished rounds are skipped
        and the interrupted round is re-launched with the same prompt (the
        engine-agnostic fallback — claude's native ``--resume`` is layered on top
        later). If only that round's reconcile Ground was lost, its Horizon result
        is recovered from disk and replayed into the Ground.

        The workspace run lock is advisory: if another *live* orchestrator is
        already driving this workspace we warn (``run.concurrent``) and proceed
        anyway, since concurrent runs are tolerated — their shared
        roadmap/blueprint writes may interleave, last-writer-wins. A crashed run
        leaves a stale lock the next run reclaims.
        """
        _lock = workspace_run_lock(
            self.workspace.state_path / "run.lock", run_id=run.id or "", exclusive=False
        )
        status = _lock.__enter__()
        try:
            return self._run_locked(
                run, dry_run=dry_run, resume=resume, concurrent_holder=status.concurrent
            )
        finally:
            _lock.__exit__(None, None, None)

    def _run_locked(
        self,
        run: RunRecord,
        *,
        dry_run: bool = False,
        resume: bool = False,
        concurrent_holder: dict | None = None,
    ) -> list[RoundReport]:
        runlog: RunLog | None = None
        if self.run_logs is not None:
            runlog = self.run_logs.get(run.id) if run.id else self.run_logs.allocate()
            run = dataclasses.replace(run, id=runlog.id)
        if self.run_store is not None and not resume:
            run = self.run_store.put(run)

        run_opening, resume_round, reconcile_only = (True, 0, False)
        if resume and runlog is not None:
            run_opening, resume_round, reconcile_only = self._resume_point(runlog)

        # Tee orchestrator events into a buffer; each `_flush_system_session`
        # writes the buffered work as a "system" session in the Log.
        self._collecting = True
        self._system_buffer.clear()
        self._open_system = None
        self._open_system_events = []
        self._emit("run.started", run_id=run.id, rounds=run.rounds_requested, dry_run=dry_run, resume=resume)
        if concurrent_holder:
            self._emit("run.concurrent", run_id=run.id, existing=concurrent_holder)
        reports: list[RoundReport] = []

        if self.start_with != "horizon" and run_opening:
            self._ground_step(run, runlog, round_index=0)
        self._publish_silent(run)
        self._flush_system_session(runlog)

        for i in range(run.rounds_requested):
            if resume and i < resume_round:
                continue  # this round finished before the interruption

            # Only the reconcile Ground was lost: replay the recovered Horizon
            # result into it rather than re-running the Horizon step, and continue
            # its native session if the engine can.
            if resume and i == resume_round and reconcile_only:
                is_last = i == run.rounds_requested - 1
                if not (is_last and self.end_with == "horizon"):
                    self._ground_step(
                        run, runlog, round_index=i + 1,
                        horizon_result=self._recover_horizon_result(runlog, i),
                        resume_session_id=self._recover_engine_session_id(runlog, 2 * i + 2),
                    )
                self._publish_silent(run)
                self._flush_system_session(runlog)
                continue

            self._sync_roadmap_tasks(run)
            self._write_blueprint_dags(rich=False)  # leandag skill reads a fresh parser DAG during horizon
            candidates = self.task_store.list()
            selected = self.scheduler.select_tasks(self.workspace, run, run.focus, candidates)
            # A focus pinned to a task that Ground has since decomposed into new
            # roadmap-derived task ids (e.g. a planning seed) selects nothing
            # runnable. Rather than stop, broaden to the focused task's project(s)
            # so the work Ground just created there runs — and say so in the log.
            if not selected and run.focus.tasks:
                fallback = self._focus_fallback(run, candidates)
                broadened = self.scheduler.select_tasks(self.workspace, run, fallback, candidates)
                if broadened:
                    self._emit(
                        "run.focus_broadened",
                        run_id=run.id,
                        round=i,
                        focus_tasks=list(run.focus.tasks),
                        projects=list(fallback.projects),
                        selected=[t.id for t in broadened],
                    )
                    selected = broadened

            if dry_run:
                self._emit("run.dry-run", planned=[t.id for t in selected])
                reports.append(RoundReport(round_index=i, planned=tuple(t.id for t in selected)))
                break

            if not selected:
                reports.append(RoundReport(round_index=i))
                self._emit("run.stopped", run_id=run.id, reason="no-runnable-tasks", round=i)
                break

            # Continue the interrupted Horizon's native session (the first task of
            # the resumed round) if the engine can; later tasks always run fresh.
            h_resume = (
                self._recover_engine_session_id(runlog, 2 * i + 1)
                if resume and i == resume_round else None
            )
            ran: list[str] = []
            blocked: list[str] = []
            last_result: HorizonResult | None = None
            for n, task in enumerate(selected):
                result = self._horizon_step(
                    run, task, runlog, round_index=i,
                    resume_session_id=h_resume if n == 0 else None,
                )
                if result is None:
                    blocked.append(task.id)
                else:
                    ran.append(task.id)
                    last_result = result
            reports.append(RoundReport(round_index=i, tasks_run=tuple(ran), tasks_blocked=tuple(blocked)))

            # A system session after the Horizon step makes the alternation
            # symmetric — [Ground, system, Horizon, system] — so the Horizon's
            # deterministic aftermath (commits, integration) is its own row in the
            # Log rather than being folded into the next Ground's system session.
            self._flush_system_session(runlog)

            # Reconcile Ground after the Horizon step, closing the G/H/G pairing —
            # unless this is the final round and the run is set to end on Horizon.
            is_last = i == run.rounds_requested - 1
            if not (is_last and self.end_with == "horizon"):
                self._ground_step(run, runlog, round_index=i + 1, horizon_result=last_result)
            self._publish_silent(run)
            self._flush_system_session(runlog)

        self._emit("run.finished", run_id=run.id)
        if not dry_run:
            outcome = integrate_workspace_run(
                self.workspace,
                run_id=run.id,
                projects=self._run_scope_projects(run),
                message=(
                    f"workspace[{run.id}] run finished\n\n"
                    f"Run: {run.id}\n"
                    f"Rounds: {run.rounds_requested}\n"
                    f"Projects: {', '.join(self._run_scope_projects(run)) or '—'}"
                ),
            )
            if outcome.error:
                self._emit("workspace.run_integrate.failed", run_id=run.id, error=outcome.error)
            elif outcome.attempted:
                self._emit("workspace.run_integrated", run_id=run.id, sha=outcome.sha, changed=outcome.changed)
        self._flush_system_session(runlog)
        self._finalize_system_session()  # close the last system session
        self._collecting = False
        return reports

    def publish(self, run: RunRecord) -> None:
        """Refresh human-facing artifacts after a collaboration boundary.

        The live dashboard reads stores directly, so the only durable file
        regenerated here is the per-project blueprint DAG
        JSON. GitHub publishing is intentionally not implicit; the GitHub inbox
        remains a shadow provider and explicit ``gh`` operations live on that
        provider.

        Publish runs once per collaboration boundary (not per Horizon step), so
        here we pay for the *rich* leandag DAG: it captures Lean source, dep/rdep
        counts, and effort, which the dashboard needs. The cheap per-step refresh
        stays parser-only.
        """
        blueprint_refs = self._write_blueprint_dags(rich=True)

        self._emit(
            "publish.completed",
            run_id=run.id,
            reports=[],
            blueprints=blueprint_refs,
        )

    def _write_blueprint_dags(self, *, rich: bool = False) -> list[str]:
        """(Re)generate ``.archon-horizon/blueprints/<project>.json`` for every project.

        Run before Horizon as well as at publish, so the leandag skill's DAG file
        is fresh when the agent consults it mid-round. The mid-round refresh uses
        the parser DAG because rich leandag scans can be very expensive on large
        workspaces; publish passes ``rich=True`` once per boundary so the cached
        DAG the dashboard serves carries Lean source and dep counts.
        """
        refs: list[str] = []
        dags = workspace_dags_rich(self.workspace) if rich else workspace_dags(self.workspace)
        if not dags:
            return refs
        out_dir = self.workspace.state_path / "blueprints"
        out_dir.mkdir(parents=True, exist_ok=True)
        for project, dag in dags.items():
            path = out_dir / f"{project}.json"
            path.write_text(json.dumps(dag, indent=2, sort_keys=True), "utf-8")
            refs.append(path.relative_to(self.workspace.root).as_posix())
        return refs
