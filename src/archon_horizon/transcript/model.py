"""Canonical transcript schema.

Every engine's native log (Claude's stream-json, Codex's ``--json`` events,
plain stdout) is normalized into this one event type. The live server tails
the resulting JSONL; the static dashboard reads the same file after the run.
There is no second "archive" format — the streamed file *is* the artifact.

A single event type with a ``kind`` discriminator keeps it trivial to encode,
diff, and render on either side.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from archon_horizon.core.clock import utc_now
from archon_horizon.core.types import Metadata


class TranscriptKind(StrEnum):
    SESSION_START = "session_start"
    THINKING = "thinking"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"
    ERROR = "error"
    SESSION_END = "session_end"


@dataclass(frozen=True, slots=True)
class TranscriptUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    reasoning_tokens_out: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class TranscriptEvent:
    kind: TranscriptKind
    at: datetime = field(default_factory=utc_now)
    text: str = ""
    tool: str = ""
    data: Metadata = field(default_factory=dict)
    usage: TranscriptUsage | None = None
