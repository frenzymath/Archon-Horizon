"""Inbox v2: per-task ownership, read-state, and task/run direct messages."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import (
    InboxFilter,
    InboxItem,
    InboxKind,
    conversation_participants,
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


def test_group_message_reaches_recipients_and_sender() -> None:
    group = _item(
        audience="task:T-7, task:T-8, human",
        provenance={"task": "T-6", "run": "0006"},
        conversation=True,
    )
    assert reaches_horizon(group, "ag-main", task="T-7", run="0007")
    assert reaches_horizon(group, "ag-main", task="T-8", run="0008")
    assert reaches_horizon(group, "ag-main", task="T-6", run="0006")
    assert not reaches_horizon(group, "ag-main", task="T-9", run="0009")
    assert conversation_participants(group) == (
        "task:T-7", "task:T-8", "human", "task:T-6",
    )


def test_explicit_started_by_routes_replies_without_provenance() -> None:
    thread = InboxItem(
        id="I-2", provider="local", kind=InboxKind.CONVERSATION,
        body="topic\n\nquestion", labels=("agent-ready",), author="horizon",
        audience="task:T-2",
        metadata={
            "conversation": True,
            "participants": ["task:T-2", "task:T-1"],
            "started_by": "task:T-1",
        },
    )
    assert conversation_participants(thread) == ("task:T-2", "task:T-1")
    assert reaches_horizon(thread, "ag-main", task="T-1")
    assert reaches_horizon(thread, "ag-main", task="T-2")
    assert not reaches_horizon(thread, "ag-main", task="T-3")


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


def test_group_dm_cli_and_reply_notify_other_participants(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "horizon-T-1")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "ag-main")

    assert main([
        "--root", str(ws), "inbox", "dm", "task:T-2", "task:T-3",
        "--body", "Coordinate proof split\n\nWhich side should each team own?", "--json",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "conversation"
    assert created["attention"] == "conversation"
    assert created["conversation"] is True
    assert created["read_by"] == ["T-1"]
    assert created["audience"] == "task:T-2, task:T-3"
    assert created["metadata"]["conversation"] is True
    assert created["metadata"]["participants"] == ["task:T-2", "task:T-3", "task:T-1"]
    assert created["metadata"]["started_by"] == "task:T-1"

    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-2")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0002")
    main(["--root", str(ws), "inbox", "list", "--json"])
    t2_payload = json.loads(capsys.readouterr().out)
    assert len(t2_payload["items"]) == 1
    assert t2_payload["attention"]["unread_conversations"][0]["id"] == "I-0001"
    main([
        "--root", str(ws), "inbox", "comment", "I-0001",
        "--body", "The right side is available; please take the descent side.",
    ])
    capsys.readouterr()

    # The initiating task is a participant, so the reply returns as unread even
    # though it was not one of the original audience recipients.
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    main(["--root", str(ws), "inbox", "list", "--unread", "--json"])
    origin_payload = json.loads(capsys.readouterr().out)
    assert origin_payload["attention"]["unread_conversations"][0]["id"] == "I-0001"

    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-9")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0009")
    main(["--root", str(ws), "inbox", "list", "--json"])
    assert json.loads(capsys.readouterr().out)["items"] == []

    inbox = _provider(ws)
    inbox.set_read("I-0001", "T-2")
    inbox.set_read("I-0001", "T-3")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    main([
        "--root", str(ws), "inbox", "comment", "I-0001",
        "--body", "I can own the descent side.",
    ])
    item = inbox.get_item("I-0001")
    assert item_readers(item) == ("T-1",)


def test_agent_list_is_open_attention_queue_and_show_acknowledges(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    main(["--root", str(ws), "inbox", "add", "--kind", "memory", "--body", "Old\n\nHistory"])
    main(["--root", str(ws), "inbox", "archive", "I-0001"])
    main(["--root", str(ws), "inbox", "add", "--body", "Advice\n\nRead when relevant"])
    main(["--root", str(ws), "inbox", "dm", "task:T-1", "--body", "Question\n\nPlease answer"])
    main(["--root", str(ws), "inbox", "protect", "--body", "Keep API\n\nDo not change Foo.bar"])
    capsys.readouterr()

    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "horizon-T-1")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "ag-main")

    main(["--root", str(ws), "inbox", "list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert [item["kind"] for item in payload["items"]] == [
        "protection", "conversation", "hint",
    ]
    assert payload["attention"]["required_protections"][0]["id"] == "I-0004"
    assert payload["attention"]["unread_conversations"][0]["id"] == "I-0003"

    main(["--root", str(ws), "inbox", "show", "I-0003", "--json"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["read_by"] == ["human", "T-1"]
    main(["--root", str(ws), "inbox", "list", "--json"])
    after = json.loads(capsys.readouterr().out)
    assert after["attention"]["unread_conversations"] == []
    # Required protections remain in the attention lane after acknowledgement.
    main(["--root", str(ws), "inbox", "read", "I-0004", "--reader", "T-1", "--json"])
    capsys.readouterr()
    main(["--root", str(ws), "inbox", "list", "--json"])
    assert json.loads(capsys.readouterr().out)["attention"]["required_protections"][0]["id"] == "I-0004"


def test_task_inbox_ref_grants_read_access_and_denied_show_exits_nonzero(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    main([
        "--root", str(ws), "task", "add", "--id", "T-1", "--project", "ag-main",
        "--objective", "Read the linked decision",
    ])
    main([
        "--root", str(ws), "inbox", "add", "--to", "human",
        "--body", "Linked decision\n\nThis item is explicitly attached to T-1.",
    ])
    main([
        "--root", str(ws), "inbox", "add", "--to", "human",
        "--body", "Unrelated decision\n\nThis item is not attached to T-1.",
    ])
    main([
        "--root", str(ws), "task", "set", "T-1", "--inbox-ref", "I-0001",
    ])
    capsys.readouterr()

    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "0001-horizon-T-1")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "ag-main")

    assert main(["--root", str(ws), "inbox", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["items"]
    assert [item["id"] for item in listed] == ["I-0001"]
    assert main(["--root", str(ws), "inbox", "show", "I-0001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "I-0001"

    assert main(["--root", str(ws), "inbox", "show", "I-0002", "--json"]) == 2
    denied = capsys.readouterr()
    assert "I-0002 is not addressed to this team" in denied.err
