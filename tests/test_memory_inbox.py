"""Memory-as-inbox: durable notes are `memory` inbox items, rendered as memory."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.base import HarnessRequest, HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

_CONFIG = """
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


def test_memory_item_feeds_memory_section(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")

    note = "induction route fails\n\ntried induction on n, fails on the succ case"
    main(["--root", str(ws), "inbox", "add", "--kind", "memory", "--to", "horizon",
          "--author", "horizon", "--body", note])

    seen: dict[str, str] = {}

    def record(req: HarnessRequest) -> HarnessResult:
        seen["prompt"] = req.prompt
        return HarnessResult(ok=True, text="done")

    local = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(ws, harnesses={"inf": NullHarness(""), "hor": NullHarness(record)}, inbox_providers=[local])
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-1", title="x", projects=("ag-main",), status=RoadmapStatus.ACTIVE),))
    )
    orch.run(RunRecord(id="", rounds_requested=1))

    prompt = seen["prompt"]
    memory_section = prompt.split("# Memory", 1)[1]
    assert note in memory_section
    # Rendered once (under Memory), not duplicated in the opened-inbox list.
    assert prompt.count(note) == 1
