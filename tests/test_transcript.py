"""Canonical transcript schema, per-engine parsers, and JSONL round-trip."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind, TranscriptUsage
from archon_horizon.transcript.parsers import (
    aggregate,
    parse_claude_line,
    parse_codex_line,
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
    assert "answer" in text and "final" in text
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
