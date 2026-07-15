"""End-to-end smoke test: a full round on the NullHarness.

Proves the engine seam works — the entire orchestration runs against an
in-process harness, so swapping in Claude Code / Codex is purely a
Harness substitution.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

from archon_horizon.agents.harness_agents import HarnessHorizonAgent, HarnessGroundAgent
from archon_horizon.core.freeze import FreezeLevel, FreezeRule, FreezeSet
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.harnesses.base import HarnessRequest, HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.orchestration.locks import InMemoryLockManager
from archon_horizon.orchestration.orchestrator import Orchestrator
from archon_horizon.orchestration.scheduler import FreezeAwareScheduler
from archon_horizon.orchestration.sync import MultiProviderSyncCoordinator
from archon_horizon.store.filesystem import (
    FilesystemEventLog,
    FilesystemMemoryStore,
    FilesystemRoadmapStore,
    FilesystemTaskStore,
)


class _RecordingHorizon(HarnessHorizonAgent):
    """A Horizon agent that, on a clean session, records a terminal status for its
    task — exactly as a real agent does by running ``horizon task set <id>
    --status <s>`` during its session. This is the ONLY way a task becomes
    terminal now: the orchestrator no longer parses the report or invents ``done``.
    ``status=None`` models a session that ended without declaring anything (so the
    machine returns the task to ``queued``)."""

    def __init__(self, harness, store, status):
        super().__init__(harness)
        self._store = store
        self._status = status

    def run_task(self, context):
        result = super().run_task(context)
        # A non-clean exit (harness FAILED) means the agent could not record
        # anything; leave it to the machine (which returns the task to queued).
        if self._status is not None and result.status is not TaskStatus.FAILED:
            task = self._store.get(context.task.id)
            self._store.put(dataclasses.replace(task, status=self._status))
            self._store.append_history(context.task.id, {
                "at": "2026-07-02T00:00:00+00:00",
                "actor": "horizon",
                "field": "status",
                "from": task.status.value,
                "to": self._status.value,
                "note": "agent recorded status via `horizon task set`",
            })
        return result


def _build(
    tmp_path: Path,
    *,
    freeze: FreezeSet | None = None,
    horizon_ok: bool = True,
    ground_ok: bool = True,
    horizon_report: str | None = None,
    horizon_sets_status: TaskStatus | None = TaskStatus.DONE,
) -> tuple[Orchestrator, FilesystemTaskStore]:
    root = tmp_path
    (root / "projects" / "ag-main").mkdir(parents=True)
    state = root / ".archon-horizon"
    workspace = Workspace(
        name="ws",
        root=root,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"))},
    )

    # Ground just reports; Horizon succeeds. Work is derived from the roadmap.
    ground_harness = (
        NullHarness("Set strategy; R-1 is active.")
        if ground_ok
        else NullHarness(lambda req: HarnessResult(ok=False, text="", metadata={"returncode": 1}))
    )

    task_store = FilesystemTaskStore(state / "tasks")
    roadmap_store = FilesystemRoadmapStore(state / "roadmap")
    horizon = _RecordingHorizon(
        NullHarness(lambda req: HarnessResult(
            ok=horizon_ok,
            text=(horizon_report or "## Summary\nDid work.") if horizon_ok else "boom",
        )),
        task_store,
        horizon_sets_status if horizon_ok else None,
    )
    # Tasks are human-created; seed one queued task directly (the NullHarness
    # can't). A matching ACTIVE roadmap milestone is kept for the tests that infer
    # a task from a roadmap id — but it no longer auto-creates any task.
    task_store.put(
        HorizonTask(
            id="R-1",
            project="ag-main",
            objective="Repair Foo.lean",
            title="Repair Foo.lean",
            projects=("ag-main",),
            status=TaskStatus.QUEUED,
            write_set=WriteSet(projects=("ag-main",)),
        )
    )
    roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(
                id="R-1",
                title="Repair Foo.lean",
                projects=("ag-main",),
                status=RoadmapStatus.ACTIVE,
            ),
        ))
    )
    orch = Orchestrator(
        workspace=workspace,
        ground=HarnessGroundAgent(ground_harness),
        horizon=horizon,
        scheduler=FreezeAwareScheduler(freeze=freeze, max_parallel=2),
        sync=MultiProviderSyncCoordinator([]),
        locks=InMemoryLockManager(),
        event_log=FilesystemEventLog(state / "events.jsonl"),
        roadmap_store=roadmap_store,
        memory_store=FilesystemMemoryStore(state / "memory.md"),
        task_store=task_store,
        freeze=freeze or FreezeSet(),
    )
    return orch, task_store


def test_round_creates_and_runs_a_task(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    run = RunRecord(id="S-0001", rounds_requested=1)

    reports = orch.run(run)

    assert len(reports) == 1
    # The derived task id mirrors the active roadmap item id.
    assert reports[0].tasks_run == ("R-1",)
    assert task_store.get("R-1").status is TaskStatus.DONE


def test_ground_crash_is_surfaced_not_swallowed(tmp_path: Path) -> None:
    # A Ground engine that exits non-zero (e.g. a hung MCP tool taking the process
    # down) used to be recorded as a healthy round with a stub report. It must now
    # surface a `ground.failed` event and mark the session failed.
    orch, _ = _build(tmp_path, ground_ok=False)

    orch.run(RunRecord(id="S-0009", rounds_requested=1))

    events = orch.event_log.read_all()
    assert any(e.type == "ground.failed" for e in events)
    failed = next(e for e in events if e.type == "ground.failed")
    assert failed.data.get("returncode") == 1


def test_healthy_ground_emits_no_failure(tmp_path: Path) -> None:
    orch, _ = _build(tmp_path)
    orch.run(RunRecord(id="S-0010", rounds_requested=1))
    assert not any(e.type == "ground.failed" for e in orch.event_log.read_all())


def test_focus_runs_in_focus_work_and_excludes_others(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    # Human-created tasks; only R-1 and R-2 are focused.
    for tid, title in (("R-2", "Repair Bar.lean"), ("R-3", "Unselected")):
        task_store.put(HorizonTask(
            id=tid, project="ag-main", objective=title, title=title,
            projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
        ))

    reports = orch.run(RunRecord(id="S-0001", focus=Focus(tasks=("R-1", "R-2")), rounds_requested=2))

    ran = [task_id for report in reports for task_id in report.tasks_run]
    # In-focus work runs; the out-of-focus task is never selected.
    assert "R-1" in ran
    assert "R-3" not in ran
    assert task_store.get("R-3").status is TaskStatus.QUEUED


def test_focused_crashed_session_retries_each_round(tmp_path: Path) -> None:
    # A non-clean session recorded no terminal status, so the machine returns the
    # task to queued and it retries each round (the multi-round re-run policy). The
    # orchestrator never writes FAILED — only the agent can, via the CLI.
    orch, task_store = _build(tmp_path, horizon_ok=False)

    reports = orch.run(RunRecord(id="S-0006", focus=Focus(tasks=("R-1",)), rounds_requested=3))

    assert tuple(task_id for report in reports for task_id in report.tasks_run) == ("R-1", "R-1", "R-1")
    assert task_store.get("R-1").status is TaskStatus.QUEUED
    # Each machine return-to-queued is auditable, not a silent flip: history
    # records the running -> queued transitions by "system".
    history = task_store.get("R-1").metadata.get("history") or []
    reopens = [h for h in history if h.get("field") == "status" and h.get("to") == "queued" and h.get("actor") == "system"]
    assert reopens, "the machine's return-to-queued of a retried task should be recorded in history"


def test_supervisor_is_horizon_only_and_never_runs_ground(tmp_path: Path) -> None:
    # The lightweight supervisor (roles=("horizon",)) runs N Horizon rounds and NEVER
    # invokes a Ground role — cleanup is the Horizon agent's own call (it spawns a
    # subagent when it wants). The task never declares terminal, so it runs each round.
    orch, _ = _build(tmp_path, horizon_sets_status=None)
    ground_calls = {"n": 0}

    def _count_ground(req):
        ground_calls["n"] += 1
        return HarnessResult(ok=True, text="should never run")

    orch.ground = HarnessGroundAgent(NullHarness(_count_ground))
    orch.roles = ("horizon",)
    orch.start_with = orch.end_with = "horizon"

    reports = orch.run(RunRecord(id="SUP-1", rounds_requested=3))

    assert len(reports) == 3
    assert all(r.tasks_run == ("R-1",) for r in reports)  # Horizon ran every round
    assert ground_calls["n"] == 0  # no Ground role in the loop


def test_focused_run_stops_early_when_task_is_done(tmp_path: Path) -> None:
    # A focused run ends as soon as its milestone is complete: the Horizon pass
    # returns DONE on round 1, so the remaining requested rounds are skipped
    # rather than reworking a finished task.
    orch, task_store = _build(tmp_path)

    reports = orch.run(RunRecord(id="S-0007", focus=Focus(tasks=("R-1",)), rounds_requested=3))

    assert tuple(task_id for report in reports for task_id in report.tasks_run) == ("R-1",)
    assert task_store.get("R-1").status is TaskStatus.DONE
    stops = [e for e in orch.event_log.read_all() if e.type == "run.stopped"]
    assert stops and stops[-1].data.get("reason") == "focus-complete"


def test_focused_success_without_status_declaration_stays_queued(tmp_path: Path) -> None:
    # A clean session that did not record a terminal status is not "done": the
    # objective may be unfinished. The machine returns the task to queued so the
    # next requested round can continue. The report text is irrelevant now.
    orch, task_store = _build(tmp_path, horizon_sets_status=None)

    reports = orch.run(RunRecord(id="S-0014", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert tuple(task_id for report in reports for task_id in report.tasks_run) == ("R-1",)
    assert task_store.get("R-1").status is TaskStatus.QUEUED
    history = task_store.get("R-1").metadata.get("history") or []
    assert any(
        h.get("field") == "status"
        and h.get("actor") == "system"
        and h.get("from") == "running"
        and h.get("to") == "queued"
        for h in history
    )
    assert any(e.type == "task.incomplete" for e in orch.event_log.read_all())


def test_done_task_is_terminal_and_never_reopened(tmp_path: Path) -> None:
    # `done` is terminal: the orchestrator never reopens a done focused task, so it
    # does not run and the run finds nothing runnable. (Re-running a finished task
    # is a deliberate human act — `horizon task set <id> --status queued` — which
    # the CLI guards separately.)
    orch, task_store = _build(tmp_path)
    task = task_store.get("R-1")
    task_store.put(dataclasses.replace(task, status=TaskStatus.DONE))
    task_store.append_history(
        "R-1",
        {
            "at": "2026-07-02T00:00:00+00:00",
            "actor": "human",
            "field": "status",
            "from": "running",
            "to": "done",
            "note": "",
        },
    )

    reports = orch.run(RunRecord(id="S-0011", focus=Focus(tasks=("R-1",)), rounds_requested=2))

    assert all("R-1" not in report.tasks_run for report in reports)
    assert task_store.get("R-1").status is TaskStatus.DONE
    history = task_store.get("R-1").metadata.get("history") or []
    assert not any(
        h.get("field") == "status" and h.get("from") == "done" and h.get("to") == "queued"
        for h in history
    ), "a done task must not be silently reopened by the orchestrator"


def test_user_started_running_task_history_explains_dispatch_queue(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    task = task_store.get("R-1")
    task_store.put(dataclasses.replace(task, status=TaskStatus.RUNNING))

    reports = orch.run(RunRecord(id="S-0015", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert reports[0].tasks_run == ("R-1",)
    history = task_store.get("R-1").metadata.get("history") or []
    assert any(
        h.get("field") == "status"
        and h.get("actor") == "system"
        and h.get("from") == "running"
        and h.get("to") == "queued"
        and "dispatched by this run" in str(h.get("note"))
        for h in history
    )


def test_dry_run_does_not_queue_focused_done_task(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    task = task_store.get("R-1")
    task_store.put(dataclasses.replace(task, status=TaskStatus.DONE))

    reports = orch.run(RunRecord(id="S-0013", focus=Focus(tasks=("R-1",)), rounds_requested=1), dry_run=True)

    assert reports[0].planned == ()
    assert task_store.get("R-1").status is TaskStatus.DONE


def test_running_horizon_is_cancelled_when_task_is_externally_closed(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    started = threading.Event()
    cancelled = threading.Event()

    def wait_for_cancel(req: HarnessRequest) -> HarnessResult:
        started.set()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if req.cancel is not None and req.cancel.is_cancelled():
                cancelled.set()
                return HarnessResult(ok=False, text="cancelled externally", metadata={"cancelled": True})
            time.sleep(0.05)
        return HarnessResult(ok=True, text="missed cancellation")

    orch.horizon = HarnessHorizonAgent(NullHarness(wait_for_cancel))

    def close_task() -> None:
        assert started.wait(timeout=2.0)
        task = task_store.get("R-1")
        task_store.put(dataclasses.replace(task, status=TaskStatus.DONE))
        task_store.append_history(
            "R-1",
            {
                "at": "2026-07-02T00:00:00+00:00",
                "actor": "human",
                "field": "status",
                "from": "running",
                "to": "done",
                "note": "",
            },
        )

    closer = threading.Thread(target=close_task)
    closer.start()
    reports = orch.run(RunRecord(id="S-0012", focus=Focus(tasks=("R-1",)), rounds_requested=1))
    closer.join(timeout=1.0)

    assert cancelled.is_set()
    assert reports[0].tasks_run == ("R-1",)
    assert task_store.get("R-1").status is TaskStatus.DONE
    assert any(
        e.type == "task.external_terminal"
        and e.data.get("task_id") == "R-1"
        and e.data.get("status") == "done"
        for e in orch.event_log.read_all()
    )


def test_unfocused_queued_task_runs_once_then_rests(tmp_path: Path) -> None:
    # Without an explicit focus, a queued task runs once and is not re-queued, so
    # later rounds find nothing runnable (the roadmap no longer re-feeds the queue).
    orch, task_store = _build(tmp_path)

    reports = orch.run(RunRecord(id="S-0008", rounds_requested=3))

    assert tuple(task_id for report in reports for task_id in report.tasks_run) == ("R-1",)
    assert task_store.get("R-1").status is TaskStatus.DONE


def test_corrupt_roadmap_item_yaml_is_restored_not_fatal(tmp_path: Path) -> None:
    # Roadmap items are human-readable YAML shards; a malformed shard must not
    # crash the run. The orchestrator continues on the last good roadmap and,
    # crucially, does NOT clobber the other on-disk shards while doing so.
    orch, _ = _build(tmp_path)
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-1", title="x", projects=("ag-main",), status=RoadmapStatus.ACTIVE),))
    )
    assert orch._load_roadmap().items  # parses cleanly and is cached as {R-1}

    # A second valid item R-2 lands on disk *after* the cache was taken, and
    # R-1's shard is independently corrupted (an agent wrote invalid YAML).
    orch.roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(id="R-1", title="x", projects=("ag-main",), status=RoadmapStatus.ACTIVE),
            RoadmapItem(id="R-2", title="keep me", projects=("ag-main",), status=RoadmapStatus.ACTIVE),
        ))
    )
    orch.roadmap_store._item_path("R-1").write_text("id: R-1\nsummary: bad: colon: here\n", "utf-8")

    restored = orch._load_roadmap()
    assert [i.id for i in restored.items] == ["R-1"]  # continued on the cached copy
    # The fallback must not delete shards absent from the cache: R-2 survives.
    assert orch.roadmap_store._item_path("R-2").exists()

    # Once the bad shard is repaired (in the store's own codec format), a clean
    # load picks up *both* items — proving R-2 was never lost to disk.
    orch.roadmap_store._item_path("R-1").write_text(
        orch.roadmap_store._codec.dumps(
            {"id": "R-1", "title": "x", "projects": ["ag-main"], "status": "active"}
        ),
        "utf-8",
    )
    assert sorted(i.id for i in orch._load_roadmap().items) == ["R-1", "R-2"]


def test_freeze_blocks_dispatch(tmp_path: Path) -> None:
    # Work is now locked at project granularity, so freeze the project.
    freeze = FreezeSet((FreezeRule(level=FreezeLevel.PROJECT, pattern="ag-main", reason="owned"),))
    orch, task_store = _build(tmp_path, freeze=freeze)
    run = RunRecord(id="S-0002", rounds_requested=1)

    reports = orch.run(run)

    # Frozen task is never selected, so it stays queued.
    assert reports[0].tasks_run == ()
    assert task_store.get("R-1").status is TaskStatus.QUEUED


def test_declaration_freeze_blocks_declared_write_set(tmp_path: Path) -> None:
    freeze = FreezeSet((FreezeRule(level=FreezeLevel.DECLARATION, pattern="Foo.bar"),))
    orch, task_store = _build(tmp_path, freeze=freeze)
    # A human task whose write-set declares the frozen declaration is not dispatched.
    task_store.put(HorizonTask(
        id="R-1", project="ag-main", objective="Repair declaration", title="Repair declaration",
        projects=("ag-main",), status=TaskStatus.QUEUED,
        write_set=WriteSet(projects=("ag-main",), declarations=("Foo.bar",)),
    ))

    reports = orch.run(RunRecord(id="S-0003", rounds_requested=1))

    assert reports[0].tasks_run == ()
    assert task_store.get("R-1").status is TaskStatus.QUEUED


def test_run_infers_a_task_from_a_roadmap_id(tmp_path: Path) -> None:
    # `horizon run <roadmap_id>` materializes a task from the milestone on demand;
    # the orchestrator never auto-creates it. Unmet deps warn but don't block.
    orch, task_store = _build(tmp_path)
    orch.roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(id="R-0", title="Prereq", projects=("ag-main",), status=RoadmapStatus.PENDING),
            RoadmapItem(id="M-1", title="Milestone", projects=("ag-main",),
                        status=RoadmapStatus.ACTIVE, depends_on=("R-0",)),
        ))
    )
    # No task exists for M-1 until it is explicitly run.
    assert not any(t.id == "M-1" for t in task_store.list())

    task = orch.ensure_roadmap_task("M-1")
    assert task is not None and task.id == "M-1"
    assert task.status is TaskStatus.QUEUED
    assert task.metadata.get("from_roadmap") is True
    assert task.roadmap_refs == ("M-1",)
    # The unmet dependency is surfaced, not enforced.
    assert any(e.type == "roadmap.deps_unmet" for e in orch.event_log.read_all())

    reports = orch.run(RunRecord(id="S-0005", focus=Focus(tasks=("M-1",)), rounds_requested=1))
    assert reports[0].tasks_run == ("M-1",)


def test_harness_request_carries_cwd(tmp_path: Path) -> None:
    seen: dict[str, HarnessRequest] = {}

    def record(req: HarnessRequest) -> HarnessResult:
        seen["req"] = req
        return HarnessResult(ok=True, text="ok")

    workspace = Workspace(
        name="ws",
        root=tmp_path,
        projects={"p": Project(name="p", path=Path("projects/p"))},
    )
    agent = HarnessHorizonAgent(NullHarness(record))
    from archon_horizon.agents.base import HorizonContext
    from archon_horizon.core.roadmap import Roadmap

    ctx = HorizonContext(
        workspace=workspace,
        run=RunRecord(id="S", rounds_requested=1),
        task=HorizonTask(id="T-1", project="p", objective="x", write_set=WriteSet()),
        roadmap=Roadmap(),
    )
    agent.run_task(ctx)
    assert seen["req"].cwd == tmp_path / "projects" / "p"


def test_extract_and_persist_ground_recommendation(tmp_path: Path) -> None:
    from archon_horizon.orchestration.orchestrator import Orchestrator
    from archon_horizon.runlog import RunLogTree

    report = """# Summary
We analyzed the codebase.

# Recommendation
## 1. Implement feature X
Do X first.
### Sub-detail
Some sub detail.

# Appendix
Other info.
"""
    extracted = Orchestrator._extract_recommendation(report)
    assert extracted == "# Recommendation\n## 1. Implement feature X\nDo X first.\n### Sub-detail\nSome sub detail."

    run_logs = RunLogTree(tmp_path / "runs")
    runlog = run_logs.allocate()
    session = runlog.new_session("ground")
    session.write_meta({"role": "ground"})
    Orchestrator._write_recommendation(session, report)

    latest = Orchestrator._latest_ground_recommendation(runlog)
    assert latest == extracted


def test_ground_recommendation_file_is_not_overwritten(tmp_path: Path) -> None:
    from archon_horizon.orchestration.orchestrator import Orchestrator
    from archon_horizon.runlog import RunLogTree

    runlog = RunLogTree(tmp_path / "runs").allocate()
    session = runlog.new_session("ground")
    explicit = "# Recommendation\n\nDetailed plan the agent wrote during the session."
    (session.path / "recommendation.md").write_text(explicit + "\n", "utf-8")

    ref = Orchestrator._write_recommendation(
        session,
        "# Summary\nGround report.\n\n# Next\n- Short final-report fallback.",
    )

    assert ref == (session.path / "recommendation.md").as_posix()
    assert (session.path / "recommendation.md").read_text("utf-8") == explicit + "\n"


def test_auth_error_early_stop(tmp_path: Path) -> None:
    orchestrator, _ = _build(tmp_path, ground_ok=False)
    # Replace ground harness with one returning auth_error
    orchestrator.ground = HarnessGroundAgent(
        NullHarness(lambda req: HarnessResult(ok=False, text="Not logged in · Please run /login", metadata={"returncode": 1, "failure_reason": "auth_error"}))
    )

    run = RunRecord(
        id="S-0099",
        focus=Focus(tasks=("R-1",)),
        rounds_requested=5,
    )
    orchestrator.run(run)

    stopped = [e.data for e in orchestrator.event_log.read_all() if e.type == "run.stopped"]
    assert len(stopped) == 1
    assert stopped[0]["reason"] == "auth_error"
