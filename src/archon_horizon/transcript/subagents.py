"""Project inline native subagent events into nested session logs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.parsers import aggregate
from archon_horizon.transcript.sink import JsonlTranscriptSink, read_transcript

_SUBAGENT_ID_KEYS = ("parent_tool_use_id", "subagent_thread_id")
_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")


def _subagent_id(event: TranscriptEvent) -> str | None:
    for key in _SUBAGENT_ID_KEYS:
        value = event.data.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _group_label(events: list[TranscriptEvent], subagent_id: str) -> str:
    """The subagent's display name: its descriptor name (``subagent_type``) taken
    from *any* event in the group — not just the first, since for codex only the
    ingested rollout events carry it. Falls back to a short id only when no event
    names the descriptor (so the log shows ``codex-1a2b3c4d`` instead of blank)."""
    for event in events:
        raw = event.data.get("subagent_type")
        if isinstance(raw, str) and raw:
            return raw
    if any("subagent_thread_id" in event.data for event in events):
        return f"codex-{subagent_id[:8]}"
    return f"native-{subagent_id[:8]}"


def _slug(value: str) -> str:
    clean = _SAFE_LABEL.sub("-", value.strip()).strip("-")
    return clean[:48] or "subagent"


def _group_native_subagent_events(events: list[TranscriptEvent]) -> list[tuple[str, str, list[TranscriptEvent]]]:
    order: list[str] = []
    grouped: dict[str, list[TranscriptEvent]] = {}
    for event in events:
        sub_id = _subagent_id(event)
        if not sub_id:
            continue
        if sub_id not in grouped:
            grouped[sub_id] = []
            order.append(sub_id)
        grouped[sub_id].append(event)
    # Label each group after collecting all its events, so a descriptor name that
    # appears on any event (not necessarily the first) wins over the id fallback.
    return [(sub_id, _group_label(grouped[sub_id], sub_id), grouped[sub_id]) for sub_id in order]


def _is_subagent_session(artifact_dir: Path) -> bool:
    meta_path = artifact_dir / "meta.json"
    if artifact_dir.parent.name == "subagents":
        return True
    if not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text("utf-8"))
    except json.JSONDecodeError:
        return False
    return meta.get("role") == "subagent"


def materialize_subagent_sessions(artifact_dir: Path) -> None:
    """Write tagged inline subagent activity as child ``subagents/*`` sessions.

    This is idempotent and can safely run both when a harness finishes and when
    the dashboard reads historical logs. It deliberately skips already-derived
    subagent sessions so child transcripts do not recursively clone themselves.
    """
    if _is_subagent_session(artifact_dir):
        return
    parent_transcript = artifact_dir / "transcript.jsonl"
    if not parent_transcript.exists():
        return
    events = read_transcript(parent_transcript)
    groups = _group_native_subagent_events(events)
    for index, (sub_id, label, child_events) in enumerate(groups, start=1):
        if not child_events:
            continue
        child_dir = artifact_dir / "subagents" / f"{index:04d}-{_slug(label)}"
        child_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = child_dir / "transcript.jsonl"
        transcript_path.unlink(missing_ok=True)
        (child_dir / "report.md").unlink(missing_ok=True)
        (child_dir / "meta.json").unlink(missing_ok=True)
        transcript = JsonlTranscriptSink(transcript_path)
        transcript.emit(TranscriptEvent(
            TranscriptKind.SESSION_START,
            data={
                "role": "subagent",
                "name": label,
                "native_subagent_id": sub_id,
                "parent_session": artifact_dir.name,
            },
        ))
        for event in child_events:
            transcript.emit(event)
        transcript.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": True}))
        report_text, usage = aggregate(child_events)
        if report_text:
            (child_dir / "report.md").write_text(report_text.rstrip() + "\n", "utf-8")
        meta = {
            "role": "subagent",
            "name": label,
            "native_subagent_id": sub_id,
            "parent_session": artifact_dir.name,
            "usage": {
                "tokens_in": usage.tokens_in,
                "tokens_out": usage.tokens_out,
                "cost_usd": usage.cost_usd,
            },
        }
        (child_dir / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True),
            "utf-8",
        )
