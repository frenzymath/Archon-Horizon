"""Canonical transcript schema, per-engine parsers, and JSONL round-trip."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind, TranscriptUsage
from archon_horizon.transcript.parsers import (
    aggregate,
    claude_session_id,
    codex_session_id,
    parse_claude_line,
    parse_codex_line,
    parse_codex_rollout_line,
    parse_plain_line,
)
from archon_horizon.transcript.pricing import (
    TokenPricing,
    estimate_cost_usd,
    with_usage_pricing,
)
from archon_horizon.transcript.sink import JsonlTranscriptSink, read_transcript


def test_plain_parser() -> None:
    assert parse_plain_line("  ") == []
    [event] = parse_plain_line("hello")
    assert event.kind is TranscriptKind.TEXT and event.text == "hello"


def test_claude_parser_maps_blocks_and_usage() -> None:
    assistant = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]},
    })
    result = json.dumps({
        "type": "result", "result": "final",
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01,
    })
    events = parse_claude_line(assistant) + parse_claude_line(result)
    kinds = [e.kind for e in events]
    assert TranscriptKind.THINKING in kinds
    assert TranscriptKind.TOOL_CALL in kinds
    text, usage = aggregate(events)
    # aggregate returns the final message (the `result` text), not every text
    # event joined — so the intermediate "answer" turn is not in the report.
    assert text == "final"
    assert usage.tokens_in == 10 and usage.tokens_out == 5 and usage.cost_usd == 0.01
    usage_events = [e for e in events if e.kind is TranscriptKind.USAGE]
    assert usage_events[0].usage == TranscriptUsage(tokens_in=10, tokens_out=5, cost_usd=0.01)


def test_codex_parser_and_garbage_is_ignored() -> None:
    msg = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
    usage = json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 3,
            "cached_input_tokens": 2,
            "output_tokens": 7,
            "reasoning_output_tokens": 1,
        },
    })
    assert parse_codex_line("not json") == []
    events = parse_codex_line(msg) + parse_codex_line(usage)
    text, u = aggregate(events)
    assert text == "hi" and u.tokens_out == 7
    assert events[-1].usage == TranscriptUsage(
        tokens_in=3,
        tokens_out=7,
        cached_tokens_in=2,
        reasoning_tokens_out=1,
        cost_usd=None,
    )


def test_codex_only_terminal_items_and_command_output() -> None:
    # item.started / item.updated are ignored so commands aren't duplicated;
    # a completed command yields a tool_call + a tool_result with its output.
    started = json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
    updated = json.dumps({"type": "item.updated", "item": {"type": "command_execution", "command": "ls"}})
    completed = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "ls", "aggregated_output": "a\nb", "exit_code": 0},
    })
    assert parse_codex_line(started) == []
    assert parse_codex_line(updated) == []
    events = parse_codex_line(completed)
    assert [e.kind for e in events] == [TranscriptKind.TOOL_CALL, TranscriptKind.TOOL_RESULT]
    assert events[0].data["command"] == "ls"
    assert events[1].data["content"] == "a\nb" and events[1].data["exit_code"] == 0


def test_codex_rollout_shell_call_is_clean_command_with_timestamp() -> None:
    # A subagent's shell call arrives as a function_call whose `arguments` is a
    # JSON *string*; it must render as a clean command (not raw JSON), and carry
    # the line's real timestamp (so events keep order instead of clustering).
    line = json.dumps({
        "timestamp": "2026-06-30T09:07:30.324Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "shell",
            "arguments": json.dumps({"cmd": ["bash", "-lc", "pwd && ls"], "workdir": "/x"}),
        },
    })
    [event] = parse_codex_rollout_line(line)
    assert event.kind is TranscriptKind.TOOL_CALL and event.tool == "Bash"
    assert event.data == {"command": "bash -lc pwd && ls"}
    assert event.at.isoformat() == "2026-06-30T09:07:30.324000+00:00"


def test_codex_rollout_non_shell_call_decodes_input_object() -> None:
    line = json.dumps({
        "type": "response_item",
        "payload": {"type": "function_call", "name": "update_plan",
                    "arguments": json.dumps({"plan": [{"step": "x", "status": "pending"}]})},
    })
    [event] = parse_codex_rollout_line(line)
    assert event.tool == "update_plan"
    assert event.data["input"] == {"plan": [{"step": "x", "status": "pending"}]}  # decoded, not a blob


def test_codex_rollout_session_meta_surfaces_model_and_role() -> None:
    line = json.dumps({
        "type": "session_meta",
        "payload": {"model": "gpt-5.5", "agent_role": "janitor", "agent_nickname": "Ramanujan"},
    })
    [event] = parse_codex_rollout_line(line)
    assert event.kind is TranscriptKind.SESSION_META
    assert event.data["model"] == "gpt-5.5" and event.data["agent_role"] == "janitor"


def test_native_session_id_extractors() -> None:
    # Claude stamps session_id on every event; codex announces it once on
    # thread.started. Both ignore unrelated lines and garbage.
    assert claude_session_id(json.dumps({"type": "system", "session_id": "s1"})) == "s1"
    assert claude_session_id(json.dumps({"type": "assistant"})) is None
    assert claude_session_id("not json") is None
    assert codex_session_id(json.dumps({"type": "thread.started", "thread_id": "t1"})) == "t1"
    assert codex_session_id(json.dumps({"type": "item.completed", "item": {}})) is None
    assert codex_session_id("not json") is None


def test_sink_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "t" / "transcript.jsonl"
    sink = JsonlTranscriptSink(path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, text="líne", usage=TranscriptUsage(tokens_in=1)))
    events = read_transcript(path)
    assert [e.kind for e in events] == [TranscriptKind.SESSION_START, TranscriptKind.TEXT]
    assert events[1].text == "líne"
    assert events[1].usage == TranscriptUsage(tokens_in=1)


def test_aggregate_reads_legacy_data_usage() -> None:
    event = TranscriptEvent(
        TranscriptKind.USAGE,
        data={"tokens_in": 2, "tokens_out": 4, "cost_usd": 0.03},
    )
    _, usage = aggregate([event])
    assert usage.tokens_in == 2
    assert usage.tokens_out == 4
    assert usage.cost_usd == 0.03


def test_usage_pricing_estimates_missing_cost() -> None:
    pricing = TokenPricing(
        input_per_million_usd=10.0,
        cached_input_per_million_usd=1.0,
        output_per_million_usd=20.0,
    )
    usage = TranscriptUsage(tokens_in=1_000_000, cached_tokens_in=250_000, tokens_out=500_000)
    assert estimate_cost_usd(usage, pricing) == 17.75

    parser = with_usage_pricing(parse_codex_line, pricing)
    [event] = parser(json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 250_000,
            "output_tokens": 500_000,
        },
    }))
    assert event.usage is not None
    assert event.usage.cost_usd == 17.75
    assert event.data["cost_estimated"] is True
