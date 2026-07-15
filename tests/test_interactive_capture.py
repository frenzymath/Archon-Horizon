"""Interactive backend that still parses: the tailer mirrors an engine's on-disk
session file into a Horizon transcript while a human drives the foreground TUI."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from archon_horizon.commands.interactive import (
    InteractiveLaunch,
    _InteractiveTailer,
    run_interactive_captured,
)
from archon_horizon.transcript.model import TranscriptKind
from archon_horizon.transcript.sink import read_transcript


class _ListSink:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event) -> None:
        self.events.append(event)


def _claude_line(**content) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [content]}})


def test_tailer_locates_and_parses_claude_session(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    proj = cfg / "projects" / "-home-x"
    proj.mkdir(parents=True)
    session = proj / "SID-1.jsonl"
    session.write_text(
        _claude_line(type="text", text="hello world") + "\n"
        + _claude_line(type="tool_use", name="Bash", input={"command": "ls"}) + "\n",
        "utf-8",
    )
    launch = InteractiveLaunch([], {"CLAUDE_CONFIG_DIR": str(cfg)}, "x", engine="claude", session_id="SID-1")
    sink = _ListSink()
    tailer = _InteractiveTailer(launch, sink)

    tailer._file = tailer._locate()
    assert tailer._file == session  # globbed by pinned session id, not cwd-sanitization
    tailer._drain()

    kinds = [e.kind for e in sink.events]
    assert TranscriptKind.TEXT in kinds
    assert TranscriptKind.TOOL_CALL in kinds
    assert any(e.tool == "Bash" for e in sink.events)


def test_tailer_is_partial_line_safe(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    proj = cfg / "projects" / "p"
    proj.mkdir(parents=True)
    session = proj / "SID-2.jsonl"
    # A half-written line (no trailing newline) must NOT be parsed yet.
    session.write_text(_claude_line(type="text", text="partial"), "utf-8")
    launch = InteractiveLaunch([], {"CLAUDE_CONFIG_DIR": str(cfg)}, "x", engine="claude", session_id="SID-2")
    sink = _ListSink()
    tailer = _InteractiveTailer(launch, sink)
    tailer._file = session

    tailer._drain()
    assert sink.events == []  # incomplete line held back

    with session.open("a", encoding="utf-8") as handle:
        handle.write("\n")  # complete the line
    tailer._drain()
    assert any(e.kind is TranscriptKind.TEXT and e.text == "partial" for e in sink.events)


def test_codex_tailer_detects_new_rollout(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    sessions = home / "sessions" / "2026" / "07" / "08"
    sessions.mkdir(parents=True)
    # A pre-existing rollout that predates the session must be ignored.
    (sessions / "rollout-old-AAA.jsonl").write_text("{}\n", "utf-8")
    launch = InteractiveLaunch([], {"CODEX_HOME": str(home)}, "x", engine="codex")
    tailer = _InteractiveTailer(launch, sink=_ListSink())  # snapshots existing rollouts

    assert tailer._locate() is None  # nothing new yet
    new = sessions / "rollout-new-BBB.jsonl"
    new.write_text("{}\n", "utf-8")
    assert tailer._locate() == new  # the freshly-created rollout is picked up


def test_run_interactive_captured_records_full_transcript(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    transcript = tmp_path / "runs" / "sessions" / "transcript.jsonl"
    # A fake "engine": writes two claude-format lines to its session file (with a
    # pause between) then exits, mimicking a foreground TUI writing its session
    # store while the human drives it.
    engine = (
        "import sys, os, json, time\n"
        "cfg, sid = sys.argv[1], sys.argv[2]\n"
        "d = os.path.join(cfg, 'projects', 'p'); os.makedirs(d, exist_ok=True)\n"
        "f = os.path.join(d, sid + '.jsonl')\n"
        "def line(**c): return json.dumps({'type':'assistant','message':{'content':[c]}})\n"
        "with open(f, 'a') as h:\n"
        "    h.write(line(type='text', text='first turn') + '\\n'); h.flush()\n"
        "    time.sleep(0.6)\n"
        "    h.write(line(type='tool_use', name='Read', input={'path':'x'}) + '\\n'); h.flush()\n"
    )
    launch = InteractiveLaunch(
        [sys.executable, "-c", engine, str(cfg), "SID-9"],
        {"CLAUDE_CONFIG_DIR": str(cfg), "PATH": os.environ.get("PATH", "")},
        "fake-claude",
        engine="claude",
        session_id="SID-9",
    )
    rc = run_interactive_captured(
        launch, tmp_path, transcript_path=transcript, role="horizon", seed_prompt="seed brief"
    )
    assert rc == 0

    events = read_transcript(transcript)
    kinds = [e.kind for e in events]
    assert kinds[0] is TranscriptKind.SESSION_START
    assert kinds[-1] is TranscriptKind.SESSION_END
    assert any(e.kind is TranscriptKind.SESSION_META and e.data.get("session_id") == "SID-9" for e in events)
    assert any(e.kind is TranscriptKind.TEXT and e.text == "seed brief" for e in events)
    assert any(e.kind is TranscriptKind.TEXT and e.text == "first turn" for e in events)
    assert any(e.kind is TranscriptKind.TOOL_CALL and e.tool == "Read" for e in events)
