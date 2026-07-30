"""Folding native subagent activity into the run transcript (Claude inline, Codex rollout files)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from archon_horizon.harnesses.command import CommandHarness
from archon_horizon.harnesses.base import HarnessRequest
from archon_horizon.harnesses.codex import CodexHarness
from archon_horizon.transcript.model import TranscriptKind
from archon_horizon.transcript.parsers import (
    codex_session_id,
    parse_claude_line,
    parse_codex_line,
    parse_codex_rollout_line,
)
from archon_horizon.transcript.sink import read_transcript


def test_claude_subagent_events_carry_parent_and_type() -> None:
    line = json.dumps({
        "type": "assistant",
        "parent_tool_use_id": "toolu_abc",
        "subagent_type": "work-reviewer",
        "message": {"content": [{"type": "text", "text": "found a sorry"}]},
    })
    (event,) = parse_claude_line(line)
    assert event.kind is TranscriptKind.TEXT and event.text == "found a sorry"
    assert event.data["parent_tool_use_id"] == "toolu_abc"
    assert event.data["subagent_type"] == "work-reviewer"
    # A parent (non-subagent) event is untagged.
    plain = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
    (pe,) = parse_claude_line(plain)
    assert "parent_tool_use_id" not in pe.data
    assert "model" not in pe.data


def test_claude_subagent_event_carries_its_own_model() -> None:
    # A subagent may run a different (e.g. cheaper) model than its parent; the
    # model is stamped on the subagent's events so the run view can show it.
    line = json.dumps({
        "type": "assistant",
        "parent_tool_use_id": "toolu_abc",
        "subagent_type": "work-reviewer",
        "message": {"model": "claude-haiku-4-5", "content": [{"type": "text", "text": "ok"}]},
    })
    (event,) = parse_claude_line(line)
    assert event.data["model"] == "claude-haiku-4-5"


def test_claude_parent_emits_dispatch_and_completion_lifecycle() -> None:
    dispatch = json.dumps({
        "type": "assistant",
        "timestamp": "2026-07-19T01:06:16.249Z",
        "message": {"content": [{
            "type": "tool_use", "id": "toolu_lane_f1", "name": "Agent",
            "input": {
                "description": "Lane F1: carve discharge",
                "subagent_type": "general-purpose",
                "model": "opus",
                "prompt": "large prompt deliberately omitted from lifecycle",
            },
        }]},
    })
    events = parse_claude_line(dispatch)
    assert [event.kind for event in events] == [TranscriptKind.TOOL_CALL, TranscriptKind.SUBAGENT_START]
    started = events[-1]
    assert started.data["name"] == "Lane F1: carve discharge"
    assert started.data["subagent_key"] == "toolu_lane_f1"
    assert started.data["model"] == "opus"
    assert "large prompt" not in started.text
    assert started.at.isoformat() == "2026-07-19T01:06:16.249000+00:00"

    notification = json.dumps({
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-07-19T02:12:33.699Z",
        "content": (
            "<task-notification>\n"
            "<tool-use-id>toolu_lane_f1</tool-use-id>\n"
            "<status>completed</status>\n"
            '<summary>Agent "Lane F1: carve discharge" finished</summary>\n'
            "</task-notification>"
        ),
    })
    (ended,) = parse_claude_line(notification)
    assert ended.kind is TranscriptKind.SUBAGENT_END
    assert ended.data["name"] == "Lane F1: carve discharge"
    assert ended.data["subagent_key"] == "toolu_lane_f1"
    assert ended.data["status"] == "completed"
    assert ended.at.isoformat() == "2026-07-19T02:12:33.699000+00:00"

    background = json.dumps({
        "type": "queue-operation", "operation": "enqueue",
        "content": (
            "<task-notification><status>completed</status>"
            '<summary>Background command "lake build" completed (exit code 0)</summary>'
            "</task-notification>"
        ),
    })
    assert parse_claude_line(background) == []


def test_claude_workflow_launch_and_completion_are_structured() -> None:
    launch = json.dumps({
        "type": "user",
        "timestamp": "2026-07-27T02:11:01Z",
        "message": {"content": [{
            "type": "tool_result", "tool_use_id": "toolu_workflow",
            "content": "Workflow launched in background.",
        }]},
        "toolUseResult": {
            "status": "async_launched",
            "taskId": "wtck0gjlg",
            "taskType": "local_workflow",
            "workflowName": "ajc-state-of-the-project",
            "runId": "wf_bed62eb6-c82",
            "summary": "Answer, with file evidence",
            "transcriptDir": "/tmp/session/subagents/workflows/wf_bed62eb6-c82",
        },
    })
    events = parse_claude_line(launch)
    progress = [event for event in events if event.kind is TranscriptKind.WORKFLOW_PROGRESS]
    assert len(progress) == 1
    assert progress[0].data["name"] == "ajc-state-of-the-project"
    assert progress[0].data["workflow_id"] == "wf_bed62eb6-c82"
    assert progress[0].data["task_id"] == "wtck0gjlg"
    assert progress[0].data["status"] == "running"

    completion = json.dumps({
        "type": "user",
        "timestamp": "2026-07-27T02:44:23Z",
        "message": {"content": (
            "<task-notification>\n"
            "<task-id>wtck0gjlg</task-id>\n"
            "<status>completed</status>\n"
            '<summary>Dynamic workflow "Answer, with file evidence" completed</summary>\n'
            "<usage><agent_count>12</agent_count><agents_done>12</agents_done>"
            "<agents_error>0</agents_error><agents_skipped>0</agents_skipped>"
            "<subagent_tokens>1670976</subagent_tokens><tool_uses>672</tool_uses>"
            "<duration_ms>2001419</duration_ms></usage>\n"
            "</task-notification>"
        )},
    })
    (ended,) = parse_claude_line(completion)
    assert ended.kind is TranscriptKind.WORKFLOW_PROGRESS
    assert ended.data["status"] == "completed"
    assert ended.data["completed_agents"] == 12
    assert ended.data["total_agents"] == 12
    assert ended.data["subagent_tokens"] == 1670976
    assert ended.data["duration_seconds"] == 2001.419


def test_claude_harness_materializes_native_subagent_session(tmp_path: Path) -> None:
    line = {
        "type": "assistant",
        "parent_tool_use_id": "toolu_abc",
        "subagent_type": "work-reviewer",
        "message": {"content": [{"type": "text", "text": "subagent final report"}]},
    }
    result = {"type": "result", "result": "parent final report", "session_id": "sess-1"}
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json\n"
        f"print(json.dumps({line!r}))\n"
        f"print(json.dumps({result!r}))\n",
        "utf-8",
    )
    harness = CommandHarness(
        "claude",
        [sys.executable, str(script)],
        parser=parse_claude_line,
    )

    outcome = harness.run(HarnessRequest(prompt="go", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert outcome.ok
    child = tmp_path / "a" / "subagents" / "0001-work-reviewer"
    assert (child / "transcript.jsonl").exists()
    assert (child / "report.md").read_text("utf-8") == "subagent final report\n"
    meta = json.loads((child / "meta.json").read_text("utf-8"))
    assert meta["role"] == "subagent"
    assert meta["native_subagent_id"] == "toolu_abc"


def test_codex_spawn_agent_surfaces_receiver_threads() -> None:
    line = json.dumps({
        "type": "item.completed",
        "item": {
            "type": "collab_tool_call",
            "tool": "spawn_agent",
            "sender_thread_id": "t-parent",
            "receiver_thread_ids": ["t-child-1", "t-child-2"],
        },
    })
    events = parse_codex_line(line)
    assert events[0].kind is TranscriptKind.TOOL_CALL and events[0].tool == "spawn_agent"
    assert events[0].data["receiver_thread_ids"] == ["t-child-1", "t-child-2"]
    assert [event.kind for event in events[1:]] == [TranscriptKind.SUBAGENT_START] * 2
    assert [event.data["subagent_key"] for event in events[1:]] == ["t-child-1", "t-child-2"]


def test_codex_rollout_emits_named_dispatch_and_terminal_status() -> None:
    dispatch = json.dumps({
        "timestamp": "2026-07-20T05:41:13.856Z",
        "type": "response_item",
        "payload": {
            "type": "function_call", "name": "spawn_agent", "call_id": "call_spawn",
            "arguments": json.dumps({"task_name": "signature_audit", "message": "secret"}),
        },
    })
    events = parse_codex_rollout_line(dispatch)
    assert [event.kind for event in events] == [TranscriptKind.TOOL_CALL, TranscriptKind.SUBAGENT_START]
    assert events[-1].data["name"] == "signature_audit"
    assert events[-1].data["subagent_key"] == "call_spawn"

    completed = json.dumps({
        "timestamp": "2026-07-20T05:50:00.000Z",
        "type": "response_item",
        "payload": {
            "type": "function_call_output", "call_id": "call_wait",
            "output": json.dumps({"status": {"child-key": {"completed": "Audit complete\nDetails"}}}),
        },
    })
    events = parse_codex_rollout_line(completed)
    assert [event.kind for event in events] == [TranscriptKind.TOOL_RESULT, TranscriptKind.SUBAGENT_END]
    assert events[-1].data["status"] == "completed"
    assert events[-1].data["summary"] == "Audit complete"

    replayed = json.dumps({
        "timestamp": "2026-07-20T05:41:13.913Z",
        "type": "event_msg",
        "payload": {"type": "task_complete", "completed_at": 1784525880, "duration_ms": 108841},
    })
    assert parse_codex_rollout_line(replayed) == []

    terminal = json.dumps({
        "timestamp": "2026-07-20T06:10:55.119Z",
        "type": "event_msg",
        "payload": {"type": "task_complete", "completed_at": 1784527855, "duration_ms": 1781193},
    })
    (ended,) = parse_codex_rollout_line(terminal)
    assert ended.kind is TranscriptKind.SUBAGENT_END
    assert ended.data["duration_seconds"] == 1781.193


def test_codex_rollout_parser_maps_substance() -> None:
    lines = [
        json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "developer",
            "content": [{"type": "input_text", "text": "INJECTED PROMPT — should be skipped"}]}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "Here is what I found."}]}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "arguments": '{"cmd":"ls"}', "call_id": "c1"}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "c1", "output": "file1\nfile2"}}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 10}}}}),
    ]
    events = [e for line in lines for e in parse_codex_rollout_line(line)]
    kinds = [e.kind for e in events]
    assert kinds == [TranscriptKind.TEXT, TranscriptKind.TOOL_CALL, TranscriptKind.TOOL_RESULT, TranscriptKind.USAGE]
    assert events[0].text == "Here is what I found."   # injected developer prompt skipped
    assert events[1].tool == "Bash"                     # exec_command unified to Bash
    assert events[3].usage.tokens_in == 100 and events[3].usage.cached_tokens_in == 10


def _fake_codex_script(tid: str) -> str:
    # Emits one parent-stream collab line spawning a child thread `tid`.
    item = {"type": "item.completed", "item": {
        "type": "collab_tool_call", "tool": "spawn_agent", "receiver_thread_ids": [tid]}}
    return "import json\nprint(json.dumps(%r))\n" % item


def test_codex_harness_ingests_child_rollout(tmp_path: Path) -> None:
    tid = "019eaa00-1111-7000-8000-abcdef012345"
    codex_home = tmp_path / "codex_home"
    sessions = codex_home / "sessions" / "2026" / "06" / "29"
    sessions.mkdir(parents=True)
    child = sessions / f"rollout-2026-06-29T10-00-00-{tid}.jsonl"
    child.write_text("\n".join([
        json.dumps({"timestamp": "2026-06-29T10:00:00Z", "type": "session_meta", "payload": {
            "source": {"subagent": {"thread_spawn": {
                "parent_thread_id": "parent", "agent_path": "/root/audit", "agent_nickname": "Harvey",
            }}},
        }}),
        json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "child subagent report"}]}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "x"}}),
        json.dumps({"timestamp": "2026-06-29T10:10:00Z", "type": "event_msg", "payload": {
            "type": "task_complete"}}),
    ]), "utf-8")

    script = tmp_path / "fake_codex.py"
    script.write_text(_fake_codex_script(tid), "utf-8")
    harness = CodexHarness(
        "codex", [sys.executable, str(script)],
        parser=parse_codex_line, session_id_of=codex_session_id, codex_home=str(codex_home),
    )
    result = harness.run(HarnessRequest(prompt="go", cwd=tmp_path, artifact_dir=tmp_path / "a"))
    assert result.ok

    events = read_transcript(tmp_path / "a" / "transcript.jsonl")
    # The parent's spawn call is present...
    assert any(e.kind is TranscriptKind.TOOL_CALL and e.tool == "spawn_agent" for e in events)
    # ...and the child's interior was folded in, attributed to its thread.
    child_texts = [e.text for e in events if e.kind is TranscriptKind.TEXT and e.data.get("subagent_thread_id") == tid]
    assert "child subagent report" in child_texts
    assert any(e.kind is TranscriptKind.SESSION_META and e.data.get("subagent_thread_id") == tid for e in events)
    assert any(
        e.kind is TranscriptKind.SUBAGENT_END
        and e.data.get("subagent_key") == tid
        and e.data.get("name") == "Harvey"
        for e in events
    )
    child_dir = tmp_path / "a" / "subagents" / "0001-Harvey"
    assert (child_dir / "report.md").read_text("utf-8") == "child subagent report\n"
