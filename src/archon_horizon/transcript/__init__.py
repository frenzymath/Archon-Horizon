"""Canonical agent transcripts: one schema, per-engine parsers, JSONL sinks."""

from __future__ import annotations

from .model import TranscriptEvent, TranscriptKind, TranscriptUsage
from .parsers import (
    TranscriptParser,
    aggregate,
    parse_claude_line,
    parse_codex_line,
    parse_plain_line,
)
from .pricing import (
    TokenPricing,
    estimate_cost_usd,
    price_event,
    pricing_from_mapping,
    with_usage_pricing,
)
from .sink import (
    JsonlTranscriptSink,
    NullTranscriptSink,
    TranscriptSink,
    event_from_dict,
    read_transcript,
)

__all__ = [
    "JsonlTranscriptSink",
    "NullTranscriptSink",
    "TranscriptEvent",
    "TranscriptKind",
    "TranscriptUsage",
    "TokenPricing",
    "TranscriptParser",
    "TranscriptSink",
    "aggregate",
    "estimate_cost_usd",
    "event_from_dict",
    "parse_claude_line",
    "parse_codex_line",
    "parse_plain_line",
    "price_event",
    "pricing_from_mapping",
    "read_transcript",
    "with_usage_pricing",
]
