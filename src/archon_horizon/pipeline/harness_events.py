"""Transient harness events consumed by the bounded Activity collector."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HarnessEventKind(StrEnum):
    SESSION_START = "session_start"
    SESSION_META = "session_meta"
    THINKING = "thinking"
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # Native delegation observations become compact parent Activity events.
    SUBAGENT_START = "subagent_start"
    SUBAGENT_END = "subagent_end"
    # Provider-reported counters for native multi-agent workflows.
    WORKFLOW_PROGRESS = "workflow_progress"
    USAGE = "usage"
    # Point-in-time context telemetry from engines that expose both the current
    # request and cumulative token counters (Codex rollout ``token_count``).
    CONTEXT = "context"
    # Automatic/manual context compaction recorded by an engine.
    COMPACTION = "compaction"
    # Provider retry notices are not terminal session failures.
    NOTICE = "notice"
    ERROR = "error"
    SESSION_END = "session_end"


@dataclass(frozen=True, slots=True)
class HarnessUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens_in: int = 0
    reasoning_tokens_out: int = 0
    cost_usd: float | None = None


@dataclass(frozen=True, slots=True)
class HarnessEvent:
    kind: HarnessEventKind
    at: datetime = field(default_factory=utc_now)
    text: str = ""
    tool: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    usage: HarnessUsage | None = None
