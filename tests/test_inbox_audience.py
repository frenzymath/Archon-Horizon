"""Inbox addressing: an optional recipient (audience) drives who sees an item."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxScope, reaches_horizon
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


def test_horizon_pulls_addressed_items_via_cli(tmp_path: Path, capsys) -> None:
    # State is pulled, not pushed: the agent reads its addressed items with
    # `horizon inbox list --to horizon --json` (the `horizon-inbox` skill's path).
    import json

    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")

    main(["--root", str(ws), "inbox", "add", "--body", "FOR_HORIZON\n\naddressed to horizon", "--to", "horizon"])
    main(["--root", str(ws), "inbox", "add", "--body", "FOR_OTHER\n\naddressed to other", "--to", "project:other"])
    capsys.readouterr()

    assert main(["--root", str(ws), "inbox", "list", "--to", "horizon", "--json"]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    bodies = " ".join(item["body"] for item in items)
    assert "FOR_HORIZON" in bodies
    assert "FOR_OTHER" not in bodies  # addressed to another project
