"""First-version publishing: runs refresh human-readable artifacts."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.null import NullHarness

CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_run_publishes_without_roadmap_markdown(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(CONFIG, "utf-8")

    orch = build_orchestrator(root, harnesses={"inf": NullHarness("Reconciled state."), "hor": NullHarness("")})
    # The Ground agent owns the roadmap on disk; here it already holds one item.
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-1", title="Formalize X", projects=("ag-main",), status=RoadmapStatus.DONE),))
    )

    reports = orch.run(RunRecord(id="", rounds_requested=1))

    assert reports[0].tasks_run == ()
    # The dashboard reads the roadmap store directly; no markdown artifact is generated.
    assert not (root / ".archon-horizon" / "reports" / "roadmap.md").exists()
    assert any(e.type == "publish.completed" for e in orch.event_log.read_all())
