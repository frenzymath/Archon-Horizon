"""Interactive backend that still parses: the tailer mirrors an engine's on-disk
session file into a Horizon transcript while a human drives the foreground TUI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.commands.run import RunCommand
from archon_horizon.commands.interactive import (
    InteractiveLaunch,
    _InteractiveTailer,
    run_interactive_captured,
)
from archon_horizon.server.service import WorkspaceService
from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.sink import JsonlTranscriptSink, read_transcript


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


def test_codex_tailer_surfaces_child_dispatch_and_completion(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    sessions = home / "sessions" / "2026" / "07" / "20"
    sessions.mkdir(parents=True)
    launch = InteractiveLaunch([], {"CODEX_HOME": str(home)}, "x", engine="codex")
    sink = _ListSink()
    tailer = _InteractiveTailer(launch, sink)  # snapshot before this session exists

    parent_id = "019f-parent"
    parent = sessions / f"rollout-parent-{parent_id}.jsonl"
    parent.write_text("\n".join([
        json.dumps({
            "timestamp": "2026-07-20T05:40:00Z", "type": "session_meta",
            "payload": {"id": parent_id, "source": "cli"},
        }),
        json.dumps({
            "timestamp": "2026-07-20T05:41:00Z", "type": "response_item",
            "payload": {
                "type": "function_call", "name": "spawn_agent", "call_id": "spawn-1",
                "arguments": json.dumps({"task_name": "signature_audit", "message": "omitted"}),
            },
        }),
    ]) + "\n", "utf-8")
    tailer._file = tailer._locate()
    assert tailer._file == parent

    child = sessions / "rollout-child.jsonl"
    child.write_text("\n".join([
        json.dumps({
            "timestamp": "2026-07-20T05:41:00Z", "type": "session_meta",
            "payload": {"source": {"subagent": {"thread_spawn": {
                "parent_thread_id": parent_id,
                "agent_path": "/root/signature_audit",
                "agent_nickname": "Harvey",
                "depth": 1,
            }}}},
        }),
        json.dumps({
            "timestamp": "2026-07-20T05:41:01Z", "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol", "effort": "ultra"},
        }),
        json.dumps({
            "timestamp": "2026-07-20T05:43:00Z", "type": "event_msg",
            "payload": {"type": "task_complete"},
        }),
    ]) + "\n", "utf-8")

    tailer._drain()
    starts = [event for event in sink.events if event.kind is TranscriptKind.SUBAGENT_START]
    ends = [event for event in sink.events if event.kind is TranscriptKind.SUBAGENT_END]
    assert len(starts) == 1 and starts[0].data["name"] == "signature_audit"
    assert len(ends) == 1
    assert ends[0].data["name"] == "signature_audit"
    assert ends[0].data["nickname"] == "Harvey"
    assert ends[0].data["model"] == "gpt-5.6-sol"
    assert ends[0].data["duration_seconds"] == 120


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


def test_interactive_run_tags_and_integrates_agent_commits(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    main(["--root", str(workspace), "init", "--no-interactive"])
    main(["--root", str(workspace), "project", "add", "proj", "projects/proj"])

    launch = InteractiveLaunch(
        ["fake-engine"],
        {
            "PATH": os.environ.get("PATH", ""),
            "GIT_AUTHOR_NAME": "interactive test",
            "GIT_AUTHOR_EMAIL": "interactive@example.com",
            "GIT_COMMITTER_NAME": "interactive test",
            "GIT_COMMITTER_EMAIL": "interactive@example.com",
        },
        "fake interactive engine",
        engine="claude",
        session_id="interactive-test-session",
    )

    import archon_horizon.commands.interactive as interactive_module

    monkeypatch.setattr(
        interactive_module,
        "interactive_launch_for_role",
        lambda *args, **kwargs: launch,
    )

    def fake_captured(actual, cwd, **kwargs):
        assert actual.env["ARCHON_HORIZON_RUN"] == "0001"
        assert actual.env["ARCHON_HORIZON_SESSION"] == "0001-horizon-interactive"
        assert actual.env["ARCHON_HORIZON_AGENT_ROLE"] == "horizon"
        assert actual.env["ARCHON_HORIZON_PROJECTS"] == "proj"
        lean = workspace / "projects" / "proj" / "Demo.lean"
        lean.write_text("theorem demo : True := by trivial\n", "utf-8")
        subprocess.run(
            [actual.env["HORIZON_GIT"], "add", "projects/proj/Demo.lean"],
            cwd=cwd,
            env=actual.env,
            check=True,
        )
        subprocess.run(
            [actual.env["HORIZON_GIT"], "commit", "-m", "Prove the interactive demo"],
            cwd=cwd,
            env=actual.env,
            check=True,
        )
        sink = JsonlTranscriptSink(kwargs["transcript_path"])
        sink.emit(TranscriptEvent(TranscriptKind.TEXT, text="## Progress\n\nInteractive proof completed."))
        return 0

    monkeypatch.setattr(interactive_module, "run_interactive_captured", fake_captured)

    RunCommand(
        workspace,
        targets=("proj",),
        backend="interactive",
        dashboard=False,
    ).run()

    service = WorkspaceService(workspace)
    view = service.session_commits_view("0001", "0001-horizon-interactive")
    by_subject = {row["subject"]: row for row in view["commits"]}
    assert by_subject["Prove the interactive demo"]["kind"] == "agent"
    assert any(row["kind"] == "integration" for row in view["commits"])
    session_meta = service.stores.run_logs.get("0001").sessions()[0].read_meta()
    assert session_meta["commit_shas"] == [by_subject["Prove the interactive demo"]["sha"]]
    report = service.report(
        ".archon-horizon/runs/0001/sessions/0001-horizon-interactive/transcript.jsonl"
    )
    assert report["markdown"].startswith("## Progress")
    integration = service._session_integrations("0001")["0001-horizon-interactive"]
    assert integration["projects"] == ["proj"]
    assert integration["workspace_commit"]
