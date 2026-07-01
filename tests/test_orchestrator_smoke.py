"""End-to-end smoke test: a full round on the NullHarness.

Proves the engine seam works — the entire orchestration runs against an
in-process harness, so swapping in Claude Code / Codex is purely a
Harness substitution.
"""

from __future__ import annotations

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


def _build(
    tmp_path: Path,
    *,
    freeze: FreezeSet | None = None,
    horizon_ok: bool = True,
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
    ground_harness = NullHarness("Set strategy; R-1 is active.")
    horizon = HarnessHorizonAgent(
        NullHarness(lambda req: HarnessResult(ok=horizon_ok, text="done" if horizon_ok else "boom"))
    )

    task_store = FilesystemTaskStore(state / "tasks")
    roadmap_store = FilesystemRoadmapStore(state / "roadmap")
    # The Ground agent recommends work by marking a roadmap item ACTIVE; the
    # NullHarness can't write files, so we seed the recommendation directly.
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


def test_focus_runs_in_focus_work_and_excludes_others(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    orch.roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(id="R-1", title="Repair Foo.lean", projects=("ag-main",), status=RoadmapStatus.ACTIVE),
            RoadmapItem(id="R-2", title="Repair Bar.lean", projects=("ag-main",), status=RoadmapStatus.ACTIVE),
            RoadmapItem(id="R-3", title="Unselected", projects=("ag-main",), status=RoadmapStatus.ACTIVE),
        ))
    )

    reports = orch.run(RunRecord(id="S-0001", focus=Focus(tasks=("R-1", "R-2")), rounds_requested=2))

    ran = [task_id for report in reports for task_id in report.tasks_run]
    # In-focus work runs; the out-of-focus item is never selected.
    assert "R-1" in ran
    assert "R-3" not in ran
    assert task_store.get("R-3").status is TaskStatus.QUEUED


def test_failed_task_retries_while_item_active(tmp_path: Path) -> None:
    # A FAILED outcome is metadata, not a gate: while the roadmap item stays
    # ACTIVE the task is re-derived and retried each round, never frozen.
    orch, task_store = _build(tmp_path, horizon_ok=False)

    reports = orch.run(RunRecord(id="S-0006", rounds_requested=3))

    assert tuple(task_id for report in reports for task_id in report.tasks_run) == ("R-1", "R-1", "R-1")
    assert task_store.get("R-1").status is TaskStatus.FAILED


def test_active_item_is_reworked_every_round_even_after_done(tmp_path: Path) -> None:
    # Status is metadata, not a gate: an ACTIVE roadmap item keeps being worked
    # each round even though every Horizon pass returns DONE — Ground stops it by
    # marking the *item* done, not by the task's status. (The run-0008 stall was
    # a DONE task wrongly freezing its still-active item.)
    orch, task_store = _build(tmp_path)

    reports = orch.run(RunRecord(id="S-0007", rounds_requested=3))

    assert tuple(task_id for report in reports for task_id in report.tasks_run) == ("R-1", "R-1", "R-1")
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
    orch.roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(
                id="R-1",
                title="Repair declaration",
                projects=("ag-main",),
                status=RoadmapStatus.ACTIVE,
                metadata={"write_set": {"declarations": ["Foo.bar"]}},
            ),
        ))
    )

    reports = orch.run(RunRecord(id="S-0003", rounds_requested=1))

    assert reports[0].tasks_run == ()
    assert task_store.get("R-1").status is TaskStatus.QUEUED


def test_roadmap_dependencies_block_then_queue(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    orch.roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(id="R-0", title="Prereq", projects=("ag-main",), status=RoadmapStatus.PENDING),
            RoadmapItem(
                id="R-1",
                title="Dependent work",
                projects=("ag-main",),
                status=RoadmapStatus.ACTIVE,
                depends_on=("R-0",),
            ),
        ))
    )

    first = orch.run(RunRecord(id="S-0004", rounds_requested=1))

    assert first[0].tasks_run == ()
    assert task_store.get("R-1").status is TaskStatus.BLOCKED

    orch.roadmap_store.save(
        Roadmap(items=(
            RoadmapItem(id="R-0", title="Prereq", projects=("ag-main",), status=RoadmapStatus.DONE),
            RoadmapItem(
                id="R-1",
                title="Dependent work",
                projects=("ag-main",),
                status=RoadmapStatus.ACTIVE,
                depends_on=("R-0",),
            ),
        ))
    )

    second = orch.run(RunRecord(id="S-0005", rounds_requested=1))

    assert second[0].tasks_run == ("R-1",)
    assert task_store.get("R-1").status is TaskStatus.DONE


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

