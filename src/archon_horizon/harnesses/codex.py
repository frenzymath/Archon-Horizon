"""Codex harness — a CommandHarness that folds in spawned subagents' logs.

Codex runs each native subagent in a *separate thread*, whose interior is
written only to that thread's own rollout file under ``$CODEX_HOME/sessions/``
(the parent ``exec --json`` stream merely shows the ``spawn_agent``/``wait``
collab call with the child thread id). So after the parent stream ends we look
up each spawned child's rollout file by thread id and ingest it into the run
transcript, attributed to the child thread.

(Claude needs no equivalent: its subagent events arrive inline in the parent
stream, already tagged with ``parent_tool_use_id`` by ``parse_claude_line``.)
"""

from __future__ import annotations

import os
from pathlib import Path

import json

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.parsers import observed_effort, observed_model, parse_codex_rollout_line
from archon_horizon.transcript.sink import TranscriptSink, read_transcript

from .base import HarnessRequest
from .command import CommandHarness


class CodexHarness(CommandHarness):
    def __init__(self, *args, codex_home: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._codex_home = codex_home

    def _sessions_dir(self) -> Path:
        home = self._codex_home or self.env_overrides.get("CODEX_HOME") or os.environ.get("CODEX_HOME")
        base = Path(home).expanduser() if home else Path.home() / ".codex"
        return base / "sessions"

    @staticmethod
    def _spawned_thread_ids(transcript: Path) -> list[str]:
        ids: list[str] = []
        for event in read_transcript(transcript):
            if event.kind is TranscriptKind.TOOL_CALL:
                for tid in event.data.get("receiver_thread_ids") or []:
                    if isinstance(tid, str) and tid and tid not in ids:
                        ids.append(tid)
        return ids

    @staticmethod
    def _rollout_for(sessions: Path, thread_id: str) -> Path | None:
        matches = sorted(sessions.glob(f"**/rollout-*-{thread_id}.jsonl"))
        return matches[-1] if matches else None  # newest, if a thread id recurs

    # Keys under which a codex rollout header may record the spawned agent's
    # descriptor name, newest-schema first. Scanned top-level and one level deep.
    _AGENT_NAME_KEYS = ("agent_role", "agent_nickname", "agent_name", "nickname")

    @staticmethod
    def _rollout_header(child: Path) -> dict:
        """The ``session_meta`` payload of a rollout — carries the model and, for a
        subagent, its role/nickname. Scans the first records rather than only line
        one, since newer codex may precede the header with other lines."""
        try:
            for i, line in enumerate(child.read_text("utf-8", errors="replace").splitlines()):
                if i > 50:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                    return obj["payload"]
        except OSError:
            pass
        return {}

    @classmethod
    def _agent_name(cls, header: dict) -> str | None:
        """The spawned agent's descriptor name from a rollout header, if present —
        e.g. ``debug``/``janitor``. Tries several keys (codex has renamed this) and
        one level of nesting (``agent``/``turn_context``)."""
        def pick(d: dict) -> str | None:
            for key in cls._AGENT_NAME_KEYS:
                v = d.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            return None
        found = pick(header)
        if found:
            return found
        for nest in ("agent", "turn_context"):
            sub = header.get(nest)
            if isinstance(sub, dict) and (found := pick(sub)):
                return found
        return None

    def _rollout_events(self, child: Path | None) -> list[TranscriptEvent]:
        """Parse a rollout file into transcript events via the same parser the
        transcript uses, so a schema quirk here can't blank the readback."""
        if child is None:
            return []
        events: list[TranscriptEvent] = []
        try:
            for line in child.read_text("utf-8", errors="replace").splitlines():
                events.extend(parse_codex_rollout_line(line))
        except OSError:
            return []
        return events

    def _rollout_model(self, child: Path | None) -> str | None:
        """The model a rollout actually used — read from its ``session_meta`` OR
        ``turn_context`` records (codex records it on the latter)."""
        return observed_model(self._rollout_events(child))

    def emit_parent_model(self, thread_id: str, sink: TranscriptSink) -> None:
        """Stamp the main session's model + reasoning effort from its own rollout
        (the ``exec --json`` stream announces neither), so the run view shows e.g.
        ``gpt-5.5`` and confirms the effort tier the engine actually applied."""
        events = self._rollout_events(self._rollout_for(self._sessions_dir(), thread_id))
        meta: dict[str, object] = {}
        model = observed_model(events)
        if isinstance(model, str) and model:
            meta["model"] = model
        effort = observed_effort(events)
        if isinstance(effort, str) and effort:
            meta["effort"] = effort
        if meta:
            sink.emit(TranscriptEvent(TranscriptKind.SESSION_META, data=meta))

    def _ingest_child(self, sessions: Path, thread_id: str, sink: TranscriptSink) -> bool:
        child = self._rollout_for(sessions, thread_id)
        if child is None:
            return False
        # Give the materialized subagent a friendly name from its rollout header
        # (the descriptor name, e.g. "janitor") instead of a bare thread id.
        header = self._rollout_header(child)
        role = self._agent_name(header)
        attrs: dict[str, object] = {"subagent_thread_id": thread_id, "source": child.as_posix()}
        if isinstance(role, str) and role:
            attrs["subagent_type"] = role
        sink.emit(TranscriptEvent(TranscriptKind.SESSION_META, data=dict(attrs)))
        for line in child.read_text("utf-8", errors="replace").splitlines():
            for event in parse_codex_rollout_line(line):
                event.data["subagent_thread_id"] = thread_id
                if "subagent_type" in attrs:
                    event.data["subagent_type"] = attrs["subagent_type"]
                sink.emit(event)
        return True

    @staticmethod
    def _parent_thread_id(transcript: Path) -> str | None:
        for event in read_transcript(transcript):
            if event.kind is TranscriptKind.SESSION_META:
                sid = event.data.get("session_id")
                if isinstance(sid, str) and sid:
                    return sid
        return None

    def _after_stream(self, request: HarnessRequest, sink: TranscriptSink) -> None:
        if request.artifact_dir is None:
            return
        transcript = request.artifact_dir / "transcript.jsonl"
        if not transcript.exists():
            return
        parent_id = self._parent_thread_id(transcript)
        if parent_id:
            self.emit_parent_model(parent_id, sink)
        thread_ids = self._spawned_thread_ids(transcript)
        sessions = self._sessions_dir()
        for thread_id in thread_ids:
            self._ingest_child(sessions, thread_id, sink)
