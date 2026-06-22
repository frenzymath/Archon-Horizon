"""First-version publishing: runs refresh human-readable artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

CONFIG = """
workspace:
  name: w
  rounds: 1
  informal_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main:
    path: projects/ag-main
    blueprint: {path: projects/ag-main/blueprint}
"""


def test_run_publishes_roadmap_blueprint_and_local_issues(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    bp = root / "projects" / "ag-main" / "blueprint"
    bp.mkdir(parents=True)
    (bp / "ch1.tex").write_text(r"\begin{lemma}\label{x}\lean{X}\leanok X.\end{lemma}", "utf-8")
    (root / "config.yaml").write_text(CONFIG, "utf-8")

    informal = NullHarness(
        """Updated state.
```json
{
  "roadmap": {"items": [{"id": "R-1", "title": "Formalize X", "projects": ["ag-main"], "status": "active"}]},
  "local_issues": [{"kind": "question", "body": "Need source citation", "project": "ag-main"}]
}
```
"""
    )
    local = FilesystemInboxProvider(root / ".archon-horizon" / "inboxes" / "local.yaml")
    orch = build_orchestrator(root, harnesses={"inf": informal, "hor": NullHarness("")}, inbox_providers=[local])

    reports = orch.run(RunRecord(id="", rounds_requested=1))

    assert reports[0].tasks_run == ()
    assert (root / ".archon-horizon" / "reports" / "roadmap.md").exists()
    assert "Formalize X" in (root / ".archon-horizon" / "reports" / "roadmap.md").read_text()
    dag_path = root / ".archon-horizon" / "blueprints" / "ag-main.json"
    assert json.loads(dag_path.read_text())["nodes"][0]["id"] == "x"
    assert local.list_items()[0].body == "Need source citation"
    assert any(e.type == "publish.completed" for e in orch.event_log.read_all())
