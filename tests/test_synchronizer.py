"""The pre-command synchronizer: an agent-session-only stderr digest."""

from __future__ import annotations

import json
import time
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import InboxDraft, InboxKind
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


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    return ws


def test_silent_for_humans(tmp_path: Path, capsys, monkeypatch) -> None:
    ws = _ws(tmp_path)
    for var in ("ARCHON_HORIZON_SESSION", "ARCHON_HORIZON_AGENT_ROLE",
                "ARCHON_HORIZON_SESSION_DIR", "ARCHON_HORIZON_TASK", "ARCHON_HORIZON_RUN"):
        monkeypatch.delenv(var, raising=False)
    main(["--root", str(ws), "inbox", "add", "--body", "Hi\n\nbody"])
    capsys.readouterr()
    main(["--root", str(ws), "inbox", "list"])
    err = capsys.readouterr().err
    assert "unread inbox" not in err  # no synchronizer chrome for a human


def test_digest_in_agent_session(tmp_path: Path, capsys, monkeypatch) -> None:
    ws = _ws(tmp_path)
    # A shared (unowned) inbox item the agent has not read.
    main(["--root", str(ws), "inbox", "add", "--body", "Heads up\n\nsomething to read"])
    # A live process marker for another run.
    runs = ws / ".archon-horizon" / "runs"
    (runs / "0007").mkdir(parents=True)
    import os, socket
    (runs / "0007" / "process.json").write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started_at": time.time()}), "utf-8"
    )
    (runs / "0007" / "run.yaml").write_text(
        "id: '0007'\nfocus:\n  task: T-2\n  tasks: [T-2]\n", "utf-8"
    )
    tasks = ws / ".archon-horizon" / "tasks"
    tasks.mkdir(parents=True)
    (tasks / "T-2.yaml").write_text(
        "id: T-2\ntitle: Representability cleanup\n", "utf-8"
    )
    # A session dir with a usage.json.
    session_dir = ws / ".archon-horizon" / "runs" / "0009" / "sessions" / "horizon-0"
    session_dir.mkdir(parents=True)
    (session_dir / "usage.json").write_text(json.dumps({"started_at": time.time() - 120, "tokens_out": 5000}), "utf-8")

    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "horizon-0")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION_DIR", str(session_dir))
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0009")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    capsys.readouterr()

    assert main(["--root", str(ws), "inbox", "list", "--json"]) == 0
    out = capsys.readouterr()
    # stdout stays pure JSON ...
    json.loads(out.out)
    # ... and the digest lands on stderr.
    assert "unread inbox" in out.err
    assert 'I-0001 "Heads up"' in out.err
    assert "Running sessions" in out.err
    assert 'task T-2 "Representability cleanup"' in out.err
    assert "horizon inbox dm task:T-2" in out.err
    assert "session running" in out.err


def test_priority_lanes_and_new_reply_invalidate_digest_cache(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    ws = _ws(tmp_path)
    main(["--root", str(ws), "inbox", "protect", "--body", "Stable API\n\nDo not change Foo.bar"])
    main([
        "--root", str(ws), "inbox", "dm", "task:T-1",
        "--body", "Need coordination\n\nCan you own the left side?",
    ])
    inbox = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local")
    inbox.set_read("I-0001", "T-1")  # protections remain required after reading

    session_dir = ws / ".archon-horizon" / "runs" / "0001" / "sessions" / "horizon-0"
    session_dir.mkdir(parents=True)
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "horizon-0")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION_DIR", str(session_dir))
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "ag-main")
    capsys.readouterr()

    main(["--root", str(ws), "inbox", "list", "--json"])
    first = capsys.readouterr()
    assert first.err.index("REQUIRED · 1 active protection") < first.err.index("ACTION · 1 unread conversation")
    payload = json.loads(first.out)
    assert payload["attention"]["required_protections"][0]["id"] == "I-0001"
    assert payload["attention"]["unread_conversations"][0]["id"] == "I-0002"

    # Populate a fresh cache, then mutate the inbox directly as another team
    # would. The next CLI command must see the new conversation immediately.
    main(["--root", str(ws), "inbox", "show", "I-0002", "--json"])
    capsys.readouterr()
    inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body="Fresh reply thread\n\nThis must bypass the ten-second cache.",
        audience="task:T-1",
        author="human",
        metadata={"conversation": True},
    ))
    main(["--root", str(ws), "inbox", "list", "--json"])
    refreshed = capsys.readouterr()
    assert 'ACTION · 1 unread conversation: I-0003 "Fresh reply thread"' in refreshed.err
