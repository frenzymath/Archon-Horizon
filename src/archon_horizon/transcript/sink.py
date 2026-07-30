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
        if not line:
            continue
        # A crashed or killed writer can leave a single malformed record; skip
        # it rather than take down every reader of the whole transcript (the
        # paginated read_transcript_page tolerates the same way).
        try:
            events.append(event_from_dict(json.loads(line)))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, ValueError):
            continue
    return events


def read_transcript_page(
    path: Path,
    *,
    before: int | None = None,
    limit: int = 120,
) -> dict[str, Any]:
    """Read one newest-first page without loading the whole JSONL file.

    ``before`` is the byte offset of the oldest event already held by the
    caller. The response events remain in canonical chronological order; the UI
    reverses them for newest-first display. Each event carries a response-only
    ``_cursor`` byte offset so live polling can merge appended events without
    duplicating older pages.
    """
    limit = max(1, min(500, int(limit)))
    try:
        file_size = path.stat().st_size
    except OSError:
        return {"events": [], "before": None, "has_more": False}
    end = file_size if before is None else max(0, min(int(before), file_size))
    if end == 0:
        return {"events": [], "before": None, "has_more": False}

    chunk_size = 64 * 1024
    start = end
    blob = b""
    try:
        with path.open("rb") as handle:
            while start > 0:
                chunk_start = max(0, start - chunk_size)
                handle.seek(chunk_start)
                blob = handle.read(start - chunk_start) + blob
                start = chunk_start
                # One delimiter may end a partial first line and another may be
                # the trailing newline, hence the extra delimiter.
                if blob.count(b"\n") >= limit + 1:
                    break
            first_is_partial = False
            if start > 0:
                handle.seek(start - 1)
                first_is_partial = handle.read(1) != b"\n"
    except OSError:
        return {"events": [], "before": None, "has_more": False}

    records: list[tuple[int, dict[str, Any]]] = []
    relative = 0
    fragments = blob.split(b"\n")
    for index, raw in enumerate(fragments):
        offset = start + relative
        relative += len(raw) + 1
        if index == 0 and first_is_partial:
            continue
        # A live writer may be between bytes of its final JSON object. The sink
        # always terminates completed events with a newline, so ignore that tail.
        if index == len(fragments) - 1 and blob and not blob.endswith(b"\n"):
            continue
        if not raw.strip():
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            records.append((offset, value))

    selected = records[-limit:]
    events = [{**value, "_cursor": offset} for offset, value in selected]
    cursor = selected[0][0] if selected else None
    return {
        "events": events,
        "before": cursor,
        "has_more": bool(cursor is not None and cursor > 0),
    }


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
