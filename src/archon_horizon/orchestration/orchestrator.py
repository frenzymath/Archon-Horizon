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
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from archon_horizon.agents.base import HorizonAgent, HorizonContext
from archon_horizon.blueprint.checks import blueprint_coverage, blueprint_lint_issues, dag_consistency_issues
from archon_horizon.blueprint.workspace import workspace_dags, workspace_dags_rich
from archon_horizon.core.clock import utc_now
from archon_horizon.core.events import Event
from archon_horizon.core.freeze import FreezeSet, frozen_violations
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxStatus, reaches_horizon
from archon_horizon.core.labels import is_agent_ready
from archon_horizon.core.permissions import WriteDomain, horizon_write_domain, ground_write_domain
from archon_horizon.core.roadmap import Roadmap, RoadmapStatus
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.sessions import RunRecord, SyncBoundary
from archon_horizon.core.status_sync import has_recorded_terminal_status
from archon_horizon.core.tasks import HorizonResult, HorizonTask, TaskStatus, WriteSet
from archon_horizon.core.workspace import Workspace
from archon_horizon.harnesses.base import Cancellation
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
    integrate_workspace_baseline,
    integrate_workspace_run,
    integrate_workspace_session,
    project_checkpoint,
)

from .scheduler import Scheduler
from .sync import SyncCoordinator


_TASK_CANCEL_POLL_S = 0.5


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


def _files_summary(value: object, *, limit: int = 8) -> str:
    """A compact "which files" fragment for a commit event, e.g.
    ``3 files: a.lean, b.tex, config.yaml`` — truncated with ``+N more``.
    Empty string when no file list is available (older events, or a no-op)."""
    if not isinstance(value, (list, tuple)):
        return ""
    files = [str(f) for f in value if f]
    if not files:
        return ""
    shown = ", ".join(files[:limit])
    if len(files) > limit:
        shown += f", +{len(files) - limit} more"
    noun = "file" if len(files) == 1 else "files"
    return f"{len(files)} {noun}: {shown}"


_FATAL_RUN_REASONS = frozenset({"auth_error", "usage_limit", "aborted_early"})


def _is_fatal_failure(meta: dict[str, object] | None) -> bool:
    """True when a session fails with unrecoverable auth/quota issues or exhausts retries."""
    if not isinstance(meta, dict):
        return False
    reason = meta.get("failure_reason")
    if reason in _FATAL_RUN_REASONS:
        return True
    if meta.get("retries_exhausted"):
        return True
    return False


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
    if event_type == "run.focus_unrunnable":
        tasks = data.get("tasks")
        if isinstance(tasks, dict) and tasks:
            detail = ", ".join(f"{tid} is {status}" for tid, status in tasks.items())
            return f"Focus task(s) not runnable ({detail}); the scheduler only runs queued tasks."
        return "The pinned focus task(s) were not runnable."
    if event_type == "ground.failed":
        reason = data.get("reason") or data.get("failure_reason")
        rc = data.get("returncode")
        bits = [b for b in (f"exit {rc}" if rc is not None else "", str(reason) if reason else "") if b]
        detail = f" ({'; '.join(bits)})" if bits else ""
        return f"Ground session crashed{detail}; the round's plan/reconcile may be incomplete."
    if event_type == "run.dry-run":
        return f"Dry run planned tasks: {_csv(data.get('planned'))}."
    if event_type == "roadmap.load_failed":
        return f"Roadmap load failed: {data.get('error')}."
    if event_type == "roadmap.restored":
        return "Restored roadmap from the last clean in-memory copy."
    if event_type == "roadmap.updated":
        return "Roadmap reloaded after Ground session."
    if event_type == "roadmap.deps_unmet":
        return (
            f"Roadmap milestone {data.get('roadmap_id')} has unmet dependencies "
            f"({_csv(data.get('dependencies'))}); running it anyway as requested."
        )
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
    if event_type == "blueprint.dags":
        projects = data.get("projects")
        if isinstance(projects, dict) and projects:
            detail = ", ".join(
                f"{project} ({c.get('nodes', 0)} nodes, {c.get('edges', 0)} edges)"
                for project, c in projects.items()
            )
            return f"Rebuilt blueprint DAG(s): {detail}."
        return "Rebuilt blueprint DAG(s)."
    if event_type == "task.created":
        return f"Created task {data.get('task_id')} for project {data.get('project')}."
    if event_type == "task.started":
        return f"Started Horizon task {data.get('task_id')}."
    if event_type == "task.finished":
        return f"Finished Horizon task {data.get('task_id')} with status: {data.get('status')}."
    if event_type == "task.incomplete":
        return (
            f"Horizon task {data.get('task_id')} did not record a terminal status; "
            "returned it to queued for the next round."
        )
    if event_type == "task.external_terminal":
        return (
            f"Stopped Horizon task {data.get('task_id')} because its stored status became "
            f"{data.get('status')} outside the running agent."
        )
    if event_type == "task.blocked":
        return f"Blocked task {data.get('task_id')}: {data.get('reason')}."
    if event_type == "task.deferred":
        return f"Deferred task {data.get('task_id')}: {data.get('reason')}."
    if event_type == "task.lock_warning":
        return (
            f"Warning: task {data.get('task_id')} overlaps a concurrent run's write set "
            "(advisory lock); ran the Horizon anyway."
        )
    if event_type == "agent.frozen":
        return f"Skipped {data.get('agent')} because that agent is frozen."
    if event_type == "write_domain.violation":
        return f"Write-domain violation for task {data.get('task_id')}: {_csv(data.get('paths'))}."
    if event_type == "project.commit":
        project = data.get("project")
        if data.get("changed"):
            files = _files_summary(data.get("files"))
            detail = f" ({files})" if files else ""
            return f"Committed {project} changes: {_short_sha(data.get('sha'))}{detail}."
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
            parts = [f"Integrated {role} session {session}: {_short_sha(sha)}"]
            projects = [p for p in (data.get("projects") or []) if p]
            if projects:
                parts.append("projects " + ", ".join(projects))
            files = _files_summary(data.get("files"))
            if files:
                parts.append(files)
            return "; ".join(parts) + "."
        return f"Integrated {role} session {session}; no workspace changes."
    if event_type == "workspace.run_baseline":
        sha = data.get("sha")
        if sha:
            return f"Recorded run {data.get('run_id')} baseline: {_short_sha(sha)}."
        return f"Recorded run {data.get('run_id')} baseline."
    if event_type == "workspace.run_baseline.failed":
        return f"Run baseline commit failed: {data.get('error')}."
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
        "task.incomplete",
        "roadmap.load_failed",
        "agent.frozen",
        "run.stopped",
        "run.focus_unrunnable",
        "ground.failed",
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
    horizon: HorizonAgent
    scheduler: Scheduler
    sync: SyncCoordinator
    event_log: EventLog
    roadmap_store: RoadmapStore
    memory_store: MemoryStore
    task_store: TaskStore
    inbox_providers: Sequence[InboxProvider] = ()
    run_store: RunStore | None = None
    run_logs: RunLogTree | None = None
    freeze: FreezeSet = field(default_factory=FreezeSet)
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
            rounds = data.get("rounds")
            resume_from = data.get("resume_from")
            if data.get("resume") and resume_from:
                suffix = f" (resuming, continuing from round {resume_from})"
            elif data.get("resume"):
                suffix = " (resuming)"
            else:
                suffix = ""
            log.header(f"Starting Run (ID: {data.get('run_id')}) - {rounds} rounds{suffix}")
        elif type == "run.finished":
            log.success(f"Run {data.get('run_id')} finished.")
        elif type == "task.started":
            log.step(f"[{actor}] Started task: {data.get('task_id')}")
        elif type == "task.finished":
            status_str = str(data.get('status')).split('.')[-1]
            log.step(f"[{actor}] Finished task: {data.get('task_id')} ({status_str})")
        elif type == "subagent.ran":
            log.step(f"[ground] Subagent '{data.get('name')}' finished (ok={data.get('ok')})")
        elif type == "task.lock_warning":
            log.warn(f"[horizon] Task {data.get('task_id')} overlaps a concurrent run's write set; "
                     "running anyway (advisory lock).")
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
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        if not git_available():
            return {}
        workspace_sha = WorkspaceGit(self.workspace.root).current_sha()
        return {"workspace_sha": workspace_sha} if workspace_sha else {}

    def _write_session_meta(self, session: SessionLog | None, meta: dict[str, object]) -> None:
        if session is None:
            return
        session.write_meta({**meta, **self._revision_meta()})

    @staticmethod
    def _agent_harness_metadata(agent: object) -> dict[str, object]:
        """Harness/model/effort descriptor for ``agent`` (harness-backed roles
        expose ``harness_metadata()``); empty for agents that don't, e.g. test
        doubles — so this only ever adds fields, never breaks the meta write."""
        describe = getattr(agent, "harness_metadata", None)
        if not callable(describe):
            return {}
        try:
            return describe()
        except Exception:
            return {}

    def _integrate_session(
        self,
        run: RunRecord,
        session: SessionLog | None,
        *,
        role: str,
        round_index: int | None = None,
        project: str | None = None,
        task_id: str | None = None,
        projects: tuple[str, ...] = (),
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
            projects=projects,
            commit_workspace=True,
        )
        self._emit(
            "workspace.session.integrated",
            run_id=run.id,
            session=session.name,
            role=role,
            project=project,
            task_id=task_id,
            projects=list(integration.projects),
            project_commits=project_commits or {},
            project_commit_errors=project_commit_errors or {},
            workspace_commit=integration.workspace_commit,
            workspace_commit_error=integration.workspace_commit_error,
            files=list(integration.workspace_files),
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

    def _agent_frozen(self, agent: str) -> bool:
        return self.freeze.agent_rule(agent) is not None

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
        cancel: Cancellation | None = None,
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
            cancel=cancel,
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
        if session is None:
            return None
        path = session.path / "recommendation.md"
        if path.is_file() and path.read_text("utf-8").strip():
            return path.as_posix()
        recommendation = cls._extract_recommendation(text)
        if not recommendation:
            return None
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

    def _queue_focus_for_round(self, run: RunRecord, *, initial: bool) -> None:
        """Queue explicitly focused tasks so a user-started run actually runs them.

        A Horizon step now leaves its task ``QUEUED`` unless the agent recorded a
        terminal status itself, so a focused task normally stays runnable on its
        own. This still re-queues a focused task that a previous run left
        ``blocked``/``failed`` (agent-reported) so an explicit ``horizon run
        <task|roadmap id>`` retries it. ``done`` is never reopened — it is terminal
        and the CLI already refuses to start a done task without an explicit
        re-queue. Dispatch records the transition to ``RUNNING`` immediately before
        the agent starts.

        Only the explicit focus is re-queued — unfocused queued work still runs
        once and then rests. Frozen tasks are left alone (the scheduler and the
        pre-dispatch freeze check handle them)."""
        for task_id in run.focus.tasks:
            try:
                task = self.task_store.get(task_id)
            except Exception:
                continue
            if frozen_violations(task.write_set, self.freeze):
                continue
            if task.status is TaskStatus.RUNNING and initial:
                self.task_store.append_history(task_id, {
                    "at": utc_now().isoformat(),
                    "actor": "system",
                    "field": "status",
                    "from": task.status.value,
                    "to": TaskStatus.QUEUED.value,
                    "note": "queued so the explicitly started task can be dispatched by this run",
                })
                self.task_store.put(dataclasses.replace(task, status=TaskStatus.QUEUED, updated_at=utc_now()))
            elif task.status is not TaskStatus.RUNNING and task.status is not TaskStatus.QUEUED:
                # A DONE the agent declared is authoritative and terminal: never
                # reopen it here. Re-running a finished task is a deliberate human
                # act (`horizon task set <id> --status queued`), guarded at the CLI.
                if task.status is TaskStatus.DONE:
                    continue
                self.task_store.append_history(task_id, {
                    "at": utc_now().isoformat(),
                    "actor": "system",
                    "field": "status",
                    "from": task.status.value,
                    "to": TaskStatus.QUEUED.value,
                    "note": "queued because the user explicitly started this task" if initial
                    else "re-queued for the next round of a focused run",
                })
                self.task_store.put(dataclasses.replace(task, status=TaskStatus.QUEUED, updated_at=utc_now()))

    def _focus_complete(self, run: RunRecord) -> bool:
        """True when a focused run's whole milestone is finished — every focused
        task has reached ``DONE``. Lets a focused ``run`` stop as soon as the work
        it was launched for is complete instead of reworking it across the
        remaining requested rounds. A FAILED/BLOCKED task is *not* complete, so
        those still retry (status stays metadata, not a freeze)."""
        if not run.focus.tasks:
            return False
        for task_id in run.focus.tasks:
            try:
                task = self.task_store.get(task_id)
            except Exception:
                return False
            if task.status is not TaskStatus.DONE:
                return False
        return True

    def _task_terminal_watcher(
        self, task_id: str, cancel: Cancellation
    ) -> tuple[threading.Event, list[TaskStatus], threading.Thread]:
        """Cancel a running Horizon harness if its task is explicitly closed."""
        stop = threading.Event()
        observed: list[TaskStatus] = []

        def watch() -> None:
            while not stop.wait(_TASK_CANCEL_POLL_S):
                try:
                    current = self.task_store.get(task_id)
                except Exception:
                    continue
                if has_recorded_terminal_status(current):
                    observed.append(current.status)
                    cancel.cancel()
                    return

        thread = threading.Thread(target=watch, name=f"horizon-task-watch-{task_id}", daemon=True)
        thread.start()
        return stop, observed, thread

    def ensure_roadmap_task(self, item_id: str) -> HorizonTask | None:
        """Infer a Horizon task from a roadmap item, on demand.

        A roadmap item is a mathematical milestone (a theorem or piece of
        infrastructure), not a task; but it carries enough scope/metadata to infer
        one. ``horizon run <roadmap_id>`` uses this to materialize a task the human
        can launch, without the orchestrator ever auto-creating tasks from the
        roadmap. Returns the stored task, or ``None`` if the item is unknown or has
        no project to run in. Unmet roadmap dependencies do not block an explicit
        run; they are only surfaced as a warning."""
        roadmap = self._load_roadmap()
        items_by_id = {item.id: item for item in roadmap.items}
        item = items_by_id.get(item_id)
        if item is None or not item.projects:
            return None
        unmet = tuple(
            dep for dep in item.depends_on
            if (dep not in items_by_id) or items_by_id[dep].status is not RoadmapStatus.DONE
        )
        if unmet:
            self._emit(
                "roadmap.deps_unmet", actor="orchestrator", roadmap_id=item.id, dependencies=unmet
            )
        objective = item.title if not item.summary else f"{item.title}\n\n{item.summary}"
        task = HorizonTask(
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
            # Link to the milestone by reference (roadmap_refs) — do NOT copy the
            # item's metadata blob. That blob carries the roadmap item's own
            # comments/history, and copying it duplicated the roadmap's comment
            # thread into the task (the "same comments in both" problem). Keep only
            # the task's own working hints.
            metadata={"roadmap_priority": item.priority, "from_roadmap": True},
        )
        existing = {t.id: t for t in self.task_store.list()}
        if item.id in existing:
            task = dataclasses.replace(task, created_at=existing[item.id].created_at)
        else:
            self._emit("task.created", actor="orchestrator", task_id=item.id, project=item.projects[0])
        return self.task_store.put(task)

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

        try:
            try:
                current_task = self.task_store.get(task.id)
            except Exception:
                current_task = task
            if has_recorded_terminal_status(current_task):
                self._emit(
                    "task.external_terminal",
                    actor="orchestrator",
                    task_id=task.id,
                    status=current_task.status.value,
                    phase="before-start",
                )
                return None

            if current_task.status is not TaskStatus.RUNNING:
                self.task_store.append_history(task.id, {
                    "at": utc_now().isoformat(),
                    "actor": "system",
                    "field": "status",
                    "from": current_task.status.value,
                    "to": TaskStatus.RUNNING.value,
                    "note": "started by Horizon run",
                })
            self.task_store.put(dataclasses.replace(current_task, status=TaskStatus.RUNNING, updated_at=utc_now()))
            self._finalize_system_session()  # close any open system session before Horizon runs
            self._emit("task.started", actor="horizon", task_id=task.id)

            session = self._session(runlog, f"horizon-{task.id}")
            started = utc_now()
            task_projects = self._task_projects(task)
            domain = horizon_write_domain(self.workspace, task_projects)
            before = self._dirty_files()
            if session is not None:
                self._write_session_meta(session, {
                    "role": "horizon",
                    "round": round_index,
                    "project": task.project,
                    "task_id": task.id,
                    "status": "running",
                    "started_at": started.isoformat(),
                    "projects": list(task_projects),
                    "dirty_at_start": sorted(before),
                    # Stamp harness/model/effort now so a RUNNING session shows the
                    # real config (e.g. ultracode) live, not a stale/empty value until
                    # it finalizes. Finalize overwrites with the observed run metadata.
                    **self._agent_harness_metadata(self.horizon),
                })
            cancel = Cancellation()
            stop_watch, externally_terminal, watcher = self._task_terminal_watcher(task.id, cancel)
            result = self.horizon.run_task(
                self._horizon_context(
                    run,
                    task,
                    domain,
                    runlog,
                    self._log_dir(session),
                    resume_session_id,
                    cancel,
                )
            )
            stop_watch.set()
            watcher.join(timeout=1.0)
            self._enforce_write_domain(task, domain, before)
            ref = self._write_run_report(session, result.report)
            if ref is not None:
                self._emit("report.written", name=f"task-{task.id}", ref=ref)
            try:
                current_task = self.task_store.get(task.id)
            except Exception:
                current_task = task
            # Whether the ENGINE exited non-cleanly. This is a harness lifecycle
            # signal (DONE == clean exit, FAILED == non-clean) — NOT a verdict on
            # the task. It only steers the post-step sync boundary below; it never
            # writes a task status, because the machine owns only queued/running.
            harness_failed = result.status is TaskStatus.FAILED
            if has_recorded_terminal_status(current_task):
                # The agent (or a subagent) recorded a terminal status
                # (done/blocked/failed) via `horizon task set` this run — that is
                # the agent's own word, and the ONLY way a task becomes terminal.
                # Never override it, and never read the report to second-guess it.
                final_status = current_task.status
                self._emit(
                    "task.external_terminal",
                    actor="orchestrator",
                    task_id=task.id,
                    status=final_status.value,
                    phase="during-run" if externally_terminal else "after-run",
                )
            else:
                # The agent did not record a terminal status, so the task is not
                # finished: the machine returns it to QUEUED for the next round.
                # The orchestrator never invents done/failed and never parses the
                # report — completion is the agent's explicit act
                # (`horizon task set <id> --status done`), verified by Ground.
                final_status = TaskStatus.QUEUED
                self._emit(
                    "task.incomplete",
                    actor="orchestrator",
                    task_id=task.id,
                    reason="no-terminal-status-recorded",
                )
                if current_task.status is not final_status:
                    self.task_store.append_history(task.id, {
                        "at": utc_now().isoformat(),
                        "actor": "system",
                        "field": "status",
                        "from": current_task.status.value,
                        "to": final_status.value,
                        "note": "Horizon session ended without the agent recording a terminal status; returned to queued",
                    })
                self.task_store.put(dataclasses.replace(current_task, status=final_status, updated_at=utc_now()))
            self._emit("task.finished", actor="horizon", task_id=task.id, status=final_status)
            if session is not None:
                self._write_session_meta(session, {
                    "role": "horizon",
                    "round": round_index,
                    "project": task.project,
                    "task_id": task.id,
                    "projects": list(task_projects),
                    "status": final_status.value,
                    "started_at": started.isoformat(),
                    "ended_at": utc_now().isoformat(),
                    "harness_name": result.metadata.get("harness_name"),
                    "harness_kind": result.metadata.get("harness_kind"),
                    "model": result.metadata.get("model"),
                    "effort": result.metadata.get("effort"),
                    "config_dir": result.metadata.get("config_dir"),
                    "auth": result.metadata.get("auth"),
                    "usage": result.metadata.get("usage"),
                    "engine_session_id": result.metadata.get("session_id"),
                })
            self._integrate_session(
                run,
                session,
                role="horizon",
                round_index=round_index,
                project=task.project,
                task_id=task.id,
                projects=task_projects,
            )

            boundary = (
                SyncBoundary.ON_BUILD_FAILURE
                if harness_failed
                else SyncBoundary.AFTER_HORIZON
            )
            self.sync.sync(boundary)
            return dataclasses.replace(result, status=final_status)
        finally:
            if "stop_watch" in locals():
                stop_watch.set()
            if "watcher" in locals():
                watcher.join(timeout=1.0)

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

    @staticmethod
    def _agent_sessions(runlog: RunLog) -> list[SessionLog]:
        """On-disk agent sessions in order, minus the interleaved system ones."""
        return [s for s in runlog.sessions() if not s.name.endswith("-system")]

    def _resume_point(self, runlog: RunLog) -> int:
        """The round to resume from: the number of completed Horizon rounds on disk.

        The horizon-only agent-session layout is ``[horizon_0, horizon_1, …]``, so a
        resume continues after the last completed one; the first incomplete session
        is the interrupted Horizon (re-queued by ``_requeue_interrupted_horizon``)."""
        sessions = self._agent_sessions(runlog)
        t = 0
        while t < len(sessions) and self._session_complete(sessions[t]):
            t += 1
        return t

    def _recover_engine_session_id(self, runlog: RunLog, session_index: int) -> str | None:
        """The native engine session id of an interrupted session, for a native
        ``--resume``. Prefer the session ``meta.json`` (written on a clean exit),
        then the transcript's ``session_end``/``session_meta`` (so a session that
        crashed mid-run is still resumable from its partial stream)."""
        from archon_horizon.transcript.sink import read_transcript

        sessions = self._agent_sessions(runlog)
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

    def _requeue_interrupted_horizon(self, runlog: RunLog, resume_round: int) -> None:
        """Re-queue the task whose Horizon session was interrupted, so a resume
        actually continues it.

        A killed Horizon step leaves its task stuck in ``RUNNING`` — it is set
        RUNNING just before the engine starts and only given a terminal status
        once the engine returns, which a crash prevents. The scheduler picks only
        ``QUEUED`` tasks, and an unfocused run has no ``focus.tasks`` for
        ``_queue_focus_for_round`` to revive, so without this the resumed round
        selects nothing and stops immediately (``no-runnable-tasks``).

        Re-queue it here so the resumed round dispatches it; ``_horizon_step``
        then continues the engine's native session (via the recovered
        ``resume_session_id``) or, for an engine without RESUME, relaunches it
        with the original prompt.
        """
        sessions = self._agent_sessions(runlog)
        idx = resume_round  # first incomplete agent session = the interrupted Horizon
        if idx >= len(sessions):
            return  # nothing was interrupted (a clean stop)
        task_id = str(sessions[idx].read_meta().get("task_id") or "")
        if not task_id:
            return
        try:
            task = self.task_store.get(task_id)
        except Exception:
            return
        if task.status is not TaskStatus.RUNNING:
            return
        self.task_store.append_history(task_id, {
            "at": utc_now().isoformat(),
            "actor": "system",
            "field": "status",
            "from": task.status.value,
            "to": TaskStatus.QUEUED.value,
            "note": "re-queued on resume: its Horizon session was interrupted mid-run",
        })
        self.task_store.put(dataclasses.replace(task, status=TaskStatus.QUEUED, updated_at=utc_now()))

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

    def run(
        self, run: RunRecord, *, dry_run: bool = False, resume: bool = False,
        rounds_override: int | None = None,
    ) -> list[RoundReport]:
        """Drive a horizon-only run: one Horizon session per round for
        ``rounds_requested`` rounds. There is no Ground role — the Horizon agent
        cleans up the workspace itself by spawning a subagent when it judges it
        useful (see the `horizon` skill). Publish is silent bookkeeping between
        steps, not a session.

        With ``resume`` the run id must already exist: a round interrupted mid-run
        is re-launched (continuing the engine's native session where possible, else
        the same prompt). Once the run has cleanly consumed its planned rounds,
        resume runs a *fresh batch* of forward rounds on the same focus (so
        ``--resume`` means "keep pushing this run"); the batch size is
        ``rounds_override`` (from ``--rounds``) when given, else the run's
        configured round count; see ``_run_locked``.

        Runs are sequential — one session at a time on the single workspace
        ledger — so there is nothing to lock against.
        """
        return self._run_locked(
            run, dry_run=dry_run, resume=resume, rounds_override=rounds_override,
        )

    def _run_locked(
        self,
        run: RunRecord,
        *,
        dry_run: bool = False,
        resume: bool = False,
        rounds_override: int | None = None,
    ) -> list[RoundReport]:
        runlog: RunLog | None = None
        if self.run_logs is not None:
            runlog = self.run_logs.get(run.id) if run.id else self.run_logs.allocate()
            run = dataclasses.replace(run, id=runlog.id)
        if self.run_store is not None and not resume:
            # Appending a hand-driven session to an existing run (via `--run`):
            # keep its original created_at so the run's position/ordering in the
            # dashboard is stable rather than jumping to "now" on every append.
            existing = None
            if run.id:
                try:
                    existing = self.run_store.get(run.id)
                except Exception:
                    existing = None
            if existing is not None:
                run = dataclasses.replace(run, created_at=existing.created_at)
            run = self.run_store.put(run)

        # How many Horizon rounds already completed on disk (0 for a fresh run);
        # resume continues from there and drives a fresh batch on top.
        resume_round = 0
        total_rounds = run.rounds_requested
        batch = run.rounds_requested
        if resume and runlog is not None:
            resume_round = self._resume_point(runlog)
            # Revive the task whose Horizon session was interrupted (left RUNNING) so
            # the resumed round can pick it up again and continue its native session.
            self._requeue_interrupted_horizon(runlog, resume_round)
            # Resume drives a fresh batch of `batch` rounds on top of what already ran
            # ("keep pushing this run"); `--rounds N` overrides the batch size.
            batch = rounds_override if rounds_override is not None else run.rounds_requested
            total_rounds = resume_round + batch

        # Tee orchestrator events into a buffer; each `_flush_system_session`
        # writes the buffered work as a "system" session in the Log.
        self._collecting = True
        self._system_buffer.clear()
        self._open_system = None
        self._open_system_events = []
        # ``rounds`` is the fresh Horizon rounds this invocation drives (the batch),
        # not the run's lifetime total, so the banner reflects the work ahead.
        rounds_to_run = batch if resume else total_rounds
        self._emit(
            "run.started", run_id=run.id, rounds=rounds_to_run, dry_run=dry_run,
            resume=resume, resume_from=resume_round if resume else None,
        )
        if not dry_run and not resume:
            baseline = integrate_workspace_baseline(
                self.workspace,
                run_id=run.id,
                projects=self._run_scope_projects(run),
            )
            if baseline.error:
                self._emit("workspace.run_baseline.failed", run_id=run.id, error=baseline.error)
            elif baseline.attempted:
                self._emit(
                    "workspace.run_baseline",
                    run_id=run.id,
                    sha=baseline.sha,
                    changed=baseline.changed,
                    files=list(baseline.files),
                    projects=list(self._run_scope_projects(run)),
                )
        reports: list[RoundReport] = []

        # Displayed round numbering is offset by ``start_round`` (0 for a normal
        # run) so a human appending sessions into an existing run gets consistent
        # session meta / commit trailers. The horizon-only session layout is
        # ``[horizon_0 (+system), horizon_1, …]``.
        base = run.start_round
        self._publish_silent(run)
        self._flush_system_session(runlog)

        for i in range(total_rounds):
            if resume and i < resume_round:
                continue  # this round finished before the interruption

            if not dry_run:
                self._queue_focus_for_round(run, initial=(not resume and i == 0))
            self._write_blueprint_dags(rich=False)  # leandag skill reads a fresh parser DAG during horizon
            candidates = self.task_store.list()
            selected = self.scheduler.select_tasks(self.workspace, run, run.focus, candidates)
            # A focus pinned to a task that is not runnable (e.g. it is frozen, or
            # names nothing that exists) selects nothing. Say *why* before the run
            # stops, so it does not look like it silently "finished early".
            if not selected and run.focus.tasks:
                by_id = {t.id: t for t in candidates}
                skipped: dict[str, str] = {}
                for tid in run.focus.tasks:
                    task = by_id.get(tid)
                    if task is None:
                        skipped[tid] = "unknown"
                    elif frozen_violations(task.write_set, self.freeze):
                        skipped[tid] = "frozen"
                    elif task.status is not TaskStatus.QUEUED:
                        skipped[tid] = task.status.value
                    else:
                        skipped[tid] = "not-runnable"
                self._emit("run.focus_unrunnable", run_id=run.id, round=i, tasks=skipped)

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
                self._recover_engine_session_id(runlog, i)
                if resume and i == resume_round else None
            )
            ran: list[str] = []
            blocked: list[str] = []
            last_result: HorizonResult | None = None
            fatal_reason: str | None = None
            for n, task in enumerate(selected):
                result = self._horizon_step(
                    run, task, runlog, round_index=base + i,
                    resume_session_id=h_resume if n == 0 else None,
                )
                if result is None:
                    blocked.append(task.id)
                else:
                    ran.append(task.id)
                    last_result = result
                    if _is_fatal_failure(result.metadata):
                        fatal_reason = str(result.metadata.get("failure_reason") or "task-failed")
                        break
            reports.append(RoundReport(round_index=i, tasks_run=tuple(ran), tasks_blocked=tuple(blocked)))

            # The Horizon's deterministic aftermath (commits, integration) is its
            # own "system" session row in the Log.
            self._flush_system_session(runlog)
            if fatal_reason is not None:
                self._emit("run.stopped", run_id=run.id, reason=fatal_reason, round=i)
                break
            self._publish_silent(run)
            self._flush_system_session(runlog)

            # Stop as soon as a focused run's milestone is complete: the reconcile
            # Ground and integration commit for this round have already run (so the
            # DONE task and the completion event are recorded in the ledger), and
            # reworking a finished task across the remaining rounds is wasted work.
            if self._focus_complete(run):
                self._emit("run.stopped", run_id=run.id, reason="focus-complete", round=i)
                break

        self._emit("run.finished", run_id=run.id)
        if not dry_run:
            outcome = integrate_workspace_run(
                self.workspace,
                run_id=run.id,
                projects=self._run_scope_projects(run),
                message=(
                    f"workspace[{run.id}] run finished\n\n"
                    f"Run: {run.id}\n"
                    f"Rounds: {total_rounds}\n"
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
        counts: dict[str, dict[str, int]] = {}
        for project, dag in dags.items():
            path = out_dir / f"{project}.json"
            # Never DOWNGRADE a rich (leandag, Lean-source-bearing) cache to the
            # cheap parser DAG: the mid-round refresh (rich=False) would otherwise
            # strip the Lean source the dashboard shows, forcing a manual
            # "Synchronize". Only overwrite a rich cache with another rich build.
            if not rich and path.exists():
                try:
                    existing = json.loads(path.read_text("utf-8"))
                    if (existing.get("meta") or {}).get("engine") == "leandag":
                        continue
                except (OSError, ValueError):
                    pass
            path.write_text(json.dumps(dag, indent=2, sort_keys=True), "utf-8")
            refs.append(path.relative_to(self.workspace.root).as_posix())
            counts[project] = {
                "nodes": len(dag.get("nodes", ())),
                "edges": len(dag.get("edges", ())),
            }
        # Only announce the rich rebuild (once per boundary); the mid-round
        # parser refresh is transient scaffolding for the leandag skill.
        if rich:
            self._emit("blueprint.dags", projects=counts)
        return refs
