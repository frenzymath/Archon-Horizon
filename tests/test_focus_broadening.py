"""A task-pinned focus must not strand the roadmap work Ground derives from it.

`horizon run <X>` pins the focus to task X, but Ground plans by activating
roadmap items, which the orchestrator queues under the ITEM ids — never X. Once X
is done (or is just a planning seed), the focus matches nothing runnable. The run
must broaden to X's project and dispatch the derived work, not stop.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus
from archon_horizon.harnesses.base import HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_focus_broadens_to_project_when_seed_is_decomposed(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")

    ran: list[str] = []

    def record_horizon(req) -> HarnessResult:
        ran.append(req.prompt)
        return HarnessResult(ok=True, text="done")

    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(
        root, harnesses={"inf": NullHarness(""), "hor": NullHarness(record_horizon)}, inbox_providers=[local]
    )
    # Ground "planned": an ACTIVE roadmap item in ag-main → a queued task R-2.
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-2", title="prove it", projects=("ag-main",), status=RoadmapStatus.ACTIVE),))
    )
    # The targeted seed task is already done — exactly the dead-focus case.
    orch.task_store.put(
        HorizonTask(id="seed", project="ag-main", objective="seed", projects=("ag-main",), status=TaskStatus.DONE)
    )

    orch.run(RunRecord(id="", focus=Focus(tasks=("seed",)), rounds_requested=1))

    events = orch.event_log.read_all()
    types = [e.type for e in events]
    # It broadened to the project and actually dispatched Horizon on the derived task.
    assert "run.focus_broadened" in types
    assert "run.stopped" not in types
    assert any(e.type == "task.finished" and e.data.get("task_id") == "R-2" for e in events)
    assert ran, "Horizon should have been dispatched on the roadmap-derived task"


def test_dead_task_focus_with_no_active_roadmap_still_stops(tmp_path: Path) -> None:
    # Broadening only rescues real roadmap work: with nothing active in the
    # project, the run still stops cleanly rather than running unrelated tasks.
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(
        root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")}, inbox_providers=[local]
    )
    orch.task_store.put(
        HorizonTask(id="seed", project="ag-main", objective="seed", projects=("ag-main",), status=TaskStatus.DONE)
    )
    orch.run(RunRecord(id="", focus=Focus(tasks=("seed",)), rounds_requested=1))
    types = [e.type for e in orch.event_log.read_all()]
    assert "run.stopped" in types
    assert "run.focus_broadened" not in types
