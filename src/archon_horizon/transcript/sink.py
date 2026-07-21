"""Transcript sinks and reader.

A sink receives canonical events as they happen. The JSONL sink appends one
line per event (append-only, so a tail -f / live poller sees it grow); the
null sink discards (used when no ``artifact_dir`` is set).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from archon_horizon.store.serde import to_jsonable

from .model import TranscriptEvent, TranscriptKind, TranscriptUsage


@runtime_checkable
class TranscriptSink(Protocol):
    def emit(self, event: TranscriptEvent) -> None: ...


class NullTranscriptSink:
    def emit(self, event: TranscriptEvent) -> None:  # noqa: D401
        return None


class JsonlTranscriptSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: TranscriptEvent) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(to_jsonable(event), ensure_ascii=False) + "\n")


def event_from_dict(data: dict[str, Any]) -> TranscriptEvent:
    raw_usage = data.get("usage")
    usage = (
        TranscriptUsage(
            tokens_in=int(raw_usage.get("tokens_in", 0)),
            tokens_out=int(raw_usage.get("tokens_out", 0)),
            cached_tokens_in=int(raw_usage.get("cached_tokens_in", 0)),
            reasoning_tokens_out=int(raw_usage.get("reasoning_tokens_out", 0)),
            cost_usd=raw_usage.get("cost_usd"),
        )
        if isinstance(raw_usage, dict)
        else None
    )
    return TranscriptEvent(
        kind=TranscriptKind(data["kind"]),
        at=datetime.fromisoformat(data["at"]),
        text=data.get("text", ""),
        tool=data.get("tool", ""),
        data=dict(data.get("data", {})),
        usage=usage,
    )


def read_transcript(path: Path) -> list[TranscriptEvent]:
    if not path.exists():
        return []
    events: list[TranscriptEvent] = []
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(event_from_dict(json.loads(line)))
    return events


def latest_report_text(path: Path) -> str:
    """Return the last substantive assistant text from a transcript.

    Interactive sessions do not have a structured harness report. Their final
    assistant message is the best report artifact, while the initial seed prompt
    is explicitly excluded.
    """
    for event in reversed(read_transcript(path)):
        if event.kind is not TranscriptKind.TEXT:
            continue
        if event.data.get("role") == "prompt":
            continue
        text = event.text.strip()
        if text:
            return text
    return ""
