"""End-to-end smoke test: a full round on the NullHarness.

Proves the engine seam works — the entire orchestration runs against an
in-process harness, so swapping in Claude Code / Codex / agy is purely a
Harness substitution.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.agents.harness_agents import HarnessHorizonAgent, HarnessInformalAgent
from archon_horizon.core.freeze import FreezeLevel, FreezeRule, FreezeSet
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
    FilesystemProposalStore,
    FilesystemRoadmapStore,
    FilesystemTaskStore,
)


def _build(tmp_path: Path, *, freeze: FreezeSet | None = None) -> tuple[Orchestrator, FilesystemTaskStore]:
    root = tmp_path
    (root / "projects" / "ag-main").mkdir(parents=True)
    state = root / ".archon-horizon"
    workspace = Workspace(
        name="ws",
        root=root,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"))},
    )

    # Informal proposes one task via a trailing json block; Horizon succeeds.
    informal_harness = NullHarness(
        'Planned one task.\n```json\n{"tasks": [{"project": "ag-main", '
        '"objective": "Repair Foo.lean", "write_set": {"files": ["Foo.lean"]}}]}\n```'
    )
    horizon = HarnessHorizonAgent(NullHarness(lambda req: HarnessResult(ok=True, text="done")))

    task_store = FilesystemTaskStore(state / "tasks")
    orch = Orchestrator(
        workspace=workspace,
        informal=HarnessInformalAgent(informal_harness),
        horizon=horizon,
        scheduler=FreezeAwareScheduler(freeze=freeze, max_parallel=2),
        sync=MultiProviderSyncCoordinator([]),
        locks=InMemoryLockManager(),
        event_log=FilesystemEventLog(state / "events.jsonl"),
        roadmap_store=FilesystemRoadmapStore(state / "roadmap.json"),
        memory_store=FilesystemMemoryStore(state / "memory.md"),
        task_store=task_store,
        proposal_store=FilesystemProposalStore(state / "proposals"),
        freeze=freeze or FreezeSet(),
    )
    return orch, task_store


def test_round_creates_and_runs_a_task(tmp_path: Path) -> None:
    orch, task_store = _build(tmp_path)
    run = RunRecord(id="S-0001", rounds_requested=1)

    reports = orch.run(run)

    assert len(reports) == 1
    assert reports[0].tasks_run == ("T-0001",)
    assert task_store.get("T-0001").status is TaskStatus.DONE


def test_freeze_blocks_dispatch(tmp_path: Path) -> None:
    freeze = FreezeSet((FreezeRule(level=FreezeLevel.FILE, pattern="Foo.lean", reason="owned"),))
    orch, task_store = _build(tmp_path, freeze=freeze)
    run = RunRecord(id="S-0002", rounds_requested=1)

    reports = orch.run(run)

    # Frozen task is never selected, so it stays queued.
    assert reports[0].tasks_run == ()
    assert task_store.get("T-0001").status is TaskStatus.QUEUED


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
