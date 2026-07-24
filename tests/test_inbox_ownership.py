"""Inbox v2: per-task ownership, read-state, and task/run direct messages."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import (
    InboxFilter,
    InboxItem,
    InboxKind,
    is_read_by,
    item_owner,
    item_readers,
    matches_filter,
    reaches_horizon,
)
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


def _item(**meta) -> InboxItem:
    return InboxItem(
        id="I-1", provider="local", kind=InboxKind.HINT, body="x", labels=(),
        audience=str(meta.pop("audience", "")), metadata=dict(meta),
    )


# ── model: ownership + DM gating ─────────────────────────────────────────

def test_owned_item_reaches_only_its_task() -> None:
    owned = _item(owner_task="T-7")
    assert reaches_horizon(owned, "ag-main", task="T-7")        # its owner
    assert not reaches_horizon(owned, "ag-main", task="T-9")    # another team
    assert reaches_horizon(owned, "ag-main")                    # see-all (task unknown)
    assert item_owner(owned) == "T-7"


def test_shared_item_reaches_everyone() -> None:
    shared = _item()  # no owner => everyone
    assert item_owner(shared) == ""
    assert reaches_horizon(shared, "ag-main", task="T-1")
    assert reaches_horizon(shared, "ag-main", task="T-2")


def test_task_and_run_direct_messages() -> None:
    assert reaches_horizon(_item(audience="task:T-7"), "ag-main", task="T-7")
    assert not reaches_horizon(_item(audience="task:T-7"), "ag-main", task="T-8")
    assert reaches_horizon(_item(audience="run:R-3"), "ag-main", run="R-3")
    assert not reaches_horizon(_item(audience="run:R-3"), "ag-main", run="R-4")
    # A DM whose recipient is unknown in this context stays hidden.
    assert not reaches_horizon(_item(audience="task:T-7"), "ag-main")


# ── model: read-state ────────────────────────────────────────────────────

def test_read_state_helpers_and_filter() -> None:
    item = _item(read_by=["T-1"])
    assert item_readers(item) == ("T-1",)
    assert is_read_by(item, "T-1")
    assert not is_read_by(item, "T-2")
    assert not is_read_by(item, "")
    assert not matches_filter(item, InboxFilter(unread_for="T-1"))  # already read
    assert matches_filter(item, InboxFilter(unread_for="T-2"))      # unread for T-2


def test_owner_filter_includes_shared() -> None:
    mine = _item(owner_task="T-1")
    theirs = _item(owner_task="T-2")
    shared = _item()
    f = InboxFilter(owner_task="T-1")
    assert matches_filter(mine, f)
    assert matches_filter(shared, f)       # a task's inbox includes shared items
    assert not matches_filter(theirs, f)   # but not another task's private item


# ── provider + CLI round-trips ───────────────────────────────────────────

def _provider(ws: Path) -> FilesystemInboxProvider:
    return FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local")


def test_set_read_and_owner_persist(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    main(["--root", str(ws), "inbox", "add", "--body", "shared note\n\nbody", "--owner", "T-1"])

    inbox = _provider(ws)
    assert item_owner(inbox.get_item("I-0001")) == "T-1"

    inbox.set_read("I-0001", "T-1")
    inbox.set_read("I-0001", "T-1")  # idempotent
    assert item_readers(inbox.get_item("I-0001")) == ("T-1",)
    inbox.set_read("I-0001", "T-1", read=False)
    assert item_readers(inbox.get_item("I-0001")) == ()

    inbox.set_owner("I-0001", "")  # share with everyone
    assert item_owner(inbox.get_item("I-0001")) == ""


def test_cli_read_unread_and_task_listing(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    main(["--root", str(ws), "inbox", "add", "--body", "PRIVATE\n\nfor T-1", "--owner", "T-1"])
    main(["--root", str(ws), "inbox", "add", "--body", "GENERAL\n\nfor all"])
    capsys.readouterr()

    # T-1's inbox = its owned item + the shared one.
    assert main(["--root", str(ws), "inbox", "list", "--task", "T-1", "--json"]) == 0
    bodies = " ".join(i["body"] for i in json.loads(capsys.readouterr().out)["items"])
    assert "PRIVATE" in bodies and "GENERAL" in bodies

    # T-2's inbox sees only the shared item, not T-1's private one.
    main(["--root", str(ws), "inbox", "list", "--task", "T-2", "--json"])
    bodies = " ".join(i["body"] for i in json.loads(capsys.readouterr().out)["items"])
    assert "PRIVATE" not in bodies and "GENERAL" in bodies

    # Mark the shared item read for reader X; read_by round-trips to JSON.
    main(["--root", str(ws), "inbox", "read", "I-0002", "--reader", "X"])
    capsys.readouterr()
    main(["--root", str(ws), "inbox", "list", "--json"])
    payload = json.loads(capsys.readouterr().out)["items"]
    shared_item = next(i for i in payload if "GENERAL" in i["body"])
    assert shared_item["read_by"] == ["X"]

    # unread-for-X now excludes the shared item; unread-for-Y still includes it.
    prov = _provider(ws)
    assert not matches_filter(prov.get_item("I-0002"), InboxFilter(unread_for="X"))
    assert matches_filter(prov.get_item("I-0002"), InboxFilter(unread_for="Y"))
