"""Per-engine native-log -> canonical event parsers.

Each parser maps ONE native output line to zero or more canonical
:class:`TranscriptEvent`s. They are best-effort and defensive: a line that
doesn't match a known shape yields nothing rather than raising, so a CLI
version bump degrades gracefully instead of crashing a run. Adjust the field
mappings here when an engine changes its schema — this is the only place that
knows a native format.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from .model import TranscriptEvent, TranscriptKind, TranscriptUsage

if TYPE_CHECKING:
    from archon_horizon.harnesses.base import Usage

TranscriptParser = Callable[[str], list[TranscriptEvent]]


def parse_plain_line(line: str) -> list[TranscriptEvent]:
    """Treat raw stdout as plain text, one event per non-empty line."""
    return [TranscriptEvent(TranscriptKind.TEXT, text=line)] if line.strip() else []


def _loads(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _int_or_zero(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _usage_from_native(data: dict) -> TranscriptUsage | None:
    """Read common native usage shapes without binding to one CLI version."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = data.get("token_usage")
    if not isinstance(usage, dict):
        usage = data if any(k in data for k in ("tokens_in", "input_tokens", "prompt_tokens")) else {}
    tokens_in = (
        usage.get("tokens_in")
        or usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("cache_creation_input_tokens")
        or 0
    )
    tokens_out = usage.get("tokens_out") or usage.get("output_tokens") or usage.get("completion_tokens") or 0
    cached_tokens_in = usage.get("cached_tokens_in") or usage.get("cached_input_tokens") or 0
    reasoning_tokens_out = usage.get("reasoning_tokens_out") or usage.get("reasoning_output_tokens") or 0
    cost_usd = (
        data.get("cost_usd")
        if data.get("cost_usd") is not None
        else data.get("total_cost_usd", usage.get("cost_usd"))
    )
    if tokens_in or tokens_out or cached_tokens_in or reasoning_tokens_out or cost_usd is not None:
        return TranscriptUsage(
            tokens_in=_int_or_zero(tokens_in),
            tokens_out=_int_or_zero(tokens_out),
            cached_tokens_in=_int_or_zero(cached_tokens_in),
            reasoning_tokens_out=_int_or_zero(reasoning_tokens_out),
            cost_usd=_float_or_none(cost_usd),
        )
    return None


def _usage_data(usage: TranscriptUsage | None) -> dict[str, object]:
    if usage is None:
        return {"tokens_in": 0, "tokens_out": 0, "cost_usd": None}
    return {
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cached_tokens_in": usage.cached_tokens_in,
        "reasoning_tokens_out": usage.reasoning_tokens_out,
        "cost_usd": usage.cost_usd,
    }


def parse_claude_line(line: str) -> list[TranscriptEvent]:
    """Claude Code ``--output-format stream-json`` lines."""
    obj = _loads(line)
    if obj is None:
        return []
    kind = obj.get("type")
    line_usage = _usage_from_native(obj)
    events: list[TranscriptEvent] = []
    if kind in ("assistant", "user"):
        message = obj.get("message", {})
        message_usage = _usage_from_native(message) if isinstance(message, dict) else None
        blocks = message.get("content", []) if isinstance(message, dict) else []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            event_usage = _usage_from_native(block) or message_usage or line_usage
            btype = block.get("type")
            if btype == "text":
                events.append(
                    TranscriptEvent(TranscriptKind.TEXT, text=block.get("text", ""), usage=event_usage)
                )
            elif btype == "thinking":
                events.append(
                    TranscriptEvent(
                        TranscriptKind.THINKING,
                        text=block.get("thinking", ""),
                        usage=event_usage,
                    )
                )
            elif btype == "tool_use":
                events.append(
                    TranscriptEvent(
                        TranscriptKind.TOOL_CALL,
                        tool=block.get("name", ""),
                        data={"input": block.get("input", {})},
                        usage=event_usage,
                    )
                )
            elif btype == "tool_result":
                events.append(
                    TranscriptEvent(
                        TranscriptKind.TOOL_RESULT,
                        data={"content": block.get("content")},
                        usage=event_usage,
                    )
                )
    elif kind == "result":
        usage = line_usage or TranscriptUsage()
        events.append(
            TranscriptEvent(
                TranscriptKind.USAGE,
                data=_usage_data(usage),
                usage=usage,
            )
        )
        if obj.get("result"):
            events.append(TranscriptEvent(TranscriptKind.TEXT, text=obj["result"]))
    return events


def parse_codex_line(line: str) -> list[TranscriptEvent]:
    """Codex ``exec --json`` thread/turn/item events."""
    obj = _loads(line)
    if obj is None:
        return []
    kind = obj.get("type", "")
    line_usage = _usage_from_native(obj)
    if kind.startswith("item."):
        item = obj.get("item", {})
        itype = item.get("type", "")
        item_usage = _usage_from_native(item) if isinstance(item, dict) else None
        event_usage = item_usage or line_usage
        if itype in ("agent_message", "message"):
            return [TranscriptEvent(TranscriptKind.TEXT, text=item.get("text", ""), usage=event_usage)]
        if itype == "reasoning":
            return [
                TranscriptEvent(TranscriptKind.THINKING, text=item.get("text", ""), usage=event_usage)
            ]
        if itype in ("command_execution", "tool_call", "file_change"):
            return [
                TranscriptEvent(
                    TranscriptKind.TOOL_CALL,
                    tool=itype,
                    data={"command": item.get("command"), "changes": item.get("changes")},
                    usage=event_usage,
                )
            ]
    elif kind == "turn.completed":
        usage = line_usage or TranscriptUsage()
        return [
            TranscriptEvent(
                TranscriptKind.USAGE,
                data=_usage_data(usage),
                usage=usage,
            )
        ]
    return []


def aggregate(events: list[TranscriptEvent]) -> tuple[str, Usage]:
    """Reduce a transcript to (final_text, usage) for the HarnessResult."""
    from archon_horizon.harnesses.base import Usage

    texts = [e.text for e in events if e.kind is TranscriptKind.TEXT and e.text]
    usage = Usage()
    for event in events:
        if event.kind is TranscriptKind.USAGE:
            if event.usage is not None:
                usage = Usage(
                    tokens_in=event.usage.tokens_in,
                    tokens_out=event.usage.tokens_out,
                    cost_usd=event.usage.cost_usd,
                )
                continue
            usage = Usage(
                tokens_in=int(event.data.get("tokens_in", 0)),
                tokens_out=int(event.data.get("tokens_out", 0)),
                cost_usd=event.data.get("cost_usd"),
            )
    return "\n".join(texts), usage
