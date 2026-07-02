"""Inbox addressing: an optional recipient (audience) drives who sees an item."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxScope, reaches_horizon
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.harnesses.base import HarnessRequest, HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider


def _item(audience: str) -> InboxItem:
    return InboxItem(id="I-1", provider="local", kind=InboxKind.HINT, body="x", labels=(), audience=audience)


def test_reaches_horizon_rules() -> None:
    assert reaches_horizon(_item(""), "ag-main")            # general
    assert reaches_horizon(_item("horizon"), "ag-main")     # addressed to it
    assert reaches_horizon(_item("project:ag-main"), "ag-main")
    assert not reaches_horizon(_item("ground"), "ag-main")
    assert not reaches_horizon(_item("human"), "ag-main")
    assert not reaches_horizon(_item("project:other"), "ag-main")
    scoped = InboxItem(
        id="I-2",
        provider="local",
        kind=InboxKind.HINT,
        body="x",
        labels=(),
        scope=InboxScope(projects=("other",)),
        audience="horizon",
    )
    assert not reaches_horizon(scoped, "ag-main")


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


def test_add_to_persists_audience(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    assert main(["--root", str(ws), "inbox", "add", "--body", "hi B\n\nfor the other project", "--to", "project:other"]) == 0
    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.audience == "project:other"


def test_horizon_only_sees_addressed_items(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")

    seen: dict[str, str] = {}

    def record(req: HarnessRequest) -> HarnessResult:
        seen["prompt"] = req.prompt
        return HarnessResult(ok=True, text="done")

    local = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local")
    main(["--root", str(ws), "inbox", "add", "--body", "FOR_HORIZON\n\naddressed to horizon", "--to", "horizon"])
    main(["--root", str(ws), "inbox", "add", "--body", "FOR_OTHER\n\naddressed to other", "--to", "project:other"])

    orch = build_orchestrator(ws, harnesses={"inf": NullHarness(""), "hor": NullHarness(record)}, inbox_providers=[local])
    orch.task_store.put(HorizonTask(
        id="R-1", project="ag-main", objective="x", title="x",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert "FOR_HORIZON" in seen["prompt"]
    assert "FOR_OTHER" not in seen["prompt"]  # addressed to another project
