"""The pre-command synchronizer: an agent-session-only stderr digest."""

from __future__ import annotations

import json
import time
from pathlib import Path

from archon_horizon.cli import main

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
    assert "1 other run" in out.err
    assert "session running" in out.err
