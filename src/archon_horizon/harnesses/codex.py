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

import dataclasses
import hashlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import json

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.parsers import (
    codex_session_id,
    observed_effort,
    observed_model,
    parse_codex_rollout_line,
)
from archon_horizon.transcript.sink import TranscriptSink, read_transcript

from .base import HarnessRequest
from .command import CommandHarness


class _CodexRolloutWatcher:
    """Tail Codex rollout files while the parent process is still running.

    The parent ``exec --json`` stream is intentionally kept as the source of
    parent conversation events. Rollout files contribute only child activity,
    parent context snapshots, and compaction rows. Source-line ids make the
    post-run reconciliation pass idempotent even when a child was tailed live.
    """

    _POLL_S = 0.5

    def __init__(self, harness: "CodexHarness", sessions: Path, sink: TranscriptSink) -> None:
        self.harness = harness
        self.sessions = sessions
        self.sink = sink
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._parent_ids: set[str] = set()
        self._root_parent_ids: set[str] = set()
        self._children: dict[str, dict[str, object]] = {}
        self._offsets: dict[Path, int] = {}
        self._line_numbers: dict[Path, int] = {}
        self._active: set[str] = set()
        self._scan_dirs: set[Path] = set()
        self._spawn_cache: dict[Path, tuple[str, dict, dict[str, object]]] = {}

    def set_parent(self, thread_id: str) -> None:
        if not thread_id:
            return
        self._parent_ids.add(thread_id)
        self._root_parent_ids.add(thread_id)
        parent_rollout = self.harness._rollout_for(self.sessions, thread_id)
        if parent_rollout is not None:
            self._scan_dirs.add(parent_rollout.parent)
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="codex-rollout-watcher", daemon=True)
            self._thread.start()

    @property
    def child_ids(self) -> tuple[str, ...]:
        return tuple(self._children)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._discover()
        self._tail()
        # A child rollout that never reached task_complete is not equivalent to
        # a successful child. Make that distinction visible at parent shutdown.
        for thread_id in sorted(self._active):
            self.harness._emit_child_lifecycle(
                self.sink, thread_id, self._children.get(thread_id, {}), "orphaned",
            )
        self._active.clear()

    @staticmethod
    def _thread_id(path: Path) -> str:
        stem = path.name.removesuffix(".jsonl")
        match = re.search(
            r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$",
            stem,
        )
        return match.group(1) if match else stem.rsplit("-", 1)[-1]

    @staticmethod
    def _spawn_info(header: dict) -> dict[str, object] | None:
        source = header.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        return spawn if isinstance(spawn, dict) else None

    def _discover(self) -> None:
        files = self._files()
        changed = True
        while changed:
            changed = False
            for path in files:
                cached = self._spawn_cache.get(path)
                if cached is None:
                    thread_id = self._thread_id(path)
                    header = self.harness._rollout_header(path)
                    if not header:
                        continue  # a just-created rollout may not have its header yet
                    spawn = self._spawn_info(header) or {}
                    self._spawn_cache[path] = (thread_id, header, spawn)
                else:
                    thread_id, header, spawn = cached
                if not thread_id or thread_id in self._root_parent_ids or thread_id in self._children:
                    continue
                parent = str(spawn.get("parent_thread_id") or "")
                if not parent or parent not in self._parent_ids:
                    continue
                parent_depth = int(self._children.get(parent, {}).get("depth") or 0)
                attrs: dict[str, object] = {
                    "parent_thread_id": parent,
                    "agent_path": spawn.get("agent_path"),
                    "agent_nickname": spawn.get("agent_nickname"),
                    "agent_role": spawn.get("agent_role"),
                    "depth": parent_depth + 1,
                    "source": path.as_posix(),
                }
                model = self.harness._rollout_model(path)
                if model:
                    attrs["model"] = model
                role = self.harness._agent_name(header)
                if role:
                    attrs["subagent_type"] = role
                self._children[thread_id] = attrs
                self._parent_ids.add(thread_id)  # enables nested discovery
                self._active.add(thread_id)
                self._scan_dirs.add(path.parent)
                self.harness._emit_child_meta(self.sink, thread_id, attrs, path)
                self.harness._emit_child_lifecycle(self.sink, thread_id, attrs, "running")
                changed = True

    def _read_new(self, path: Path) -> list[tuple[int, str]]:
        offset = self._offsets.get(path, 0)
        line_no = self._line_numbers.get(path, 0)
        rows: list[tuple[int, str]] = []
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                while raw := handle.readline():
                    if not raw.endswith(b"\n"):
                        break  # retry a partially written JSONL record next poll
                    offset += len(raw)
                    line_no += 1
                    rows.append((line_no, raw.decode("utf-8", errors="replace").rstrip("\r\n")))
        except OSError:
            return []
        self._offsets[path] = offset
        self._line_numbers[path] = line_no
        return rows

    def _files(self) -> list[Path]:
        try:
            if self._scan_dirs:
                return sorted({
                    path
                    for directory in self._scan_dirs
                    for path in directory.glob("rollout-*.jsonl")
                })
            files = sorted(self.sessions.glob("**/rollout-*.jsonl"))
            for path in files:
                if self._thread_id(path) in self._root_parent_ids:
                    self._scan_dirs.add(path.parent)
            return files
        except OSError:
            return []

    def _tail(self) -> None:
        for path in self._files():
            thread_id = self._thread_id(path)
            if thread_id not in self._children and thread_id not in self._root_parent_ids:
                continue
            rows = self._read_new(path)
            for line_no, line in rows:
                events = parse_codex_rollout_line(line)
                if thread_id in self._children:
                    attrs = self._children[thread_id]
                    for event_index, event in enumerate(events):
                        if event.kind is TranscriptKind.SUBAGENT_START:
                            # The header-derived lifecycle row is authoritative;
                            # avoid a duplicate function-call dispatch row.
                            continue
                        self.harness._emit_child_event(
                            self.sink, thread_id, attrs, event, path, line_no, line, event_index,
                        )
                        if event.kind is TranscriptKind.SUBAGENT_END:
                            self._active.discard(thread_id)
                elif thread_id in self._root_parent_ids:
                    for event_index, event in enumerate(events):
                        if event.kind is TranscriptKind.COMPACTION:
                            self.harness._emit_source_event(
                                self.sink, event, path, line_no, line, event_index,
                            )
                        elif event.kind is TranscriptKind.USAGE and "cumulative_tokens_in" in event.data:
                            context = TranscriptEvent(
                                TranscriptKind.CONTEXT,
                                at=event.at,
                                text="Context usage",
                                data=dict(event.data),
                            )
                            self.harness._emit_source_event(
                                self.sink, context, path, line_no, line, event_index,
                            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._discover()
                self._tail()
            except Exception:
                pass  # telemetry must never terminate the engine process
            self._stop.wait(self._POLL_S)


class CodexHarness(CommandHarness):
    def __init__(self, *args, codex_home: str | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._codex_home = codex_home
        self._watcher: _CodexRolloutWatcher | None = None
        self._source_seen: set[str] = set()
        self._last_compaction_at: dict[Path, datetime] = {}

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
            with child.open("r", encoding="utf-8", errors="replace") as handle:
                for i, line in enumerate(handle):
                    line = line.rstrip("\r\n")
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
        # Codex 0.144+ records the descriptor under the spawn provenance rather
        # than as a top-level agent_role.
        source = header.get("source")
        subagent = source.get("subagent") if isinstance(source, dict) else None
        spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
        if isinstance(spawn, dict):
            nickname = spawn.get("agent_nickname")
            if isinstance(nickname, str) and nickname.strip():
                return nickname.strip()
            agent_path = spawn.get("agent_path")
            if isinstance(agent_path, str) and agent_path.strip():
                return agent_path.rstrip("/").rsplit("/", 1)[-1]
        return None

    @staticmethod
    def _source_event_id(source: Path, line_no: int, line: str, event_index: int = 0) -> str:
        raw = f"{source}:{line_no}:{event_index}:{line}".encode("utf-8", errors="replace")
        return hashlib.sha256(raw).hexdigest()

    def _emit_source_event(
        self, sink: TranscriptSink, event: TranscriptEvent, source: Path, line_no: int, line: str,
        event_index: int = 0,
    ) -> bool:
        event_id = self._source_event_id(source, line_no, line, event_index)
        if event_id in self._source_seen:
            return False
        self._source_seen.add(event_id)
        if event.kind is TranscriptKind.COMPACTION:
            previous = self._last_compaction_at.get(source)
            if previous is not None and abs((event.at - previous).total_seconds()) <= 1.0:
                return False
            self._last_compaction_at[source] = event.at
        event.data["codex_event_id"] = event_id
        event.data["codex_source"] = source.as_posix()
        sink.emit(event)
        usage_file = getattr(self, "_usage_file", None)
        if usage_file is not None and (
            event.kind is TranscriptKind.USAGE
            or "subagent_thread_id" not in event.data
        ):
            usage_file.add(event)
        return True

    def _emit_child_lifecycle(
        self, sink: TranscriptSink, thread_id: str, attrs: dict[str, object], status: str,
    ) -> None:
        name = str(attrs.get("subagent_type") or attrs.get("agent_nickname") or "subagent")
        if status == "running":
            data = {
                "lifecycle": "subagent", "status": status, "name": name, "engine": "codex",
                "subagent_key": thread_id, "subagent_thread_id": thread_id,
                **{k: v for k, v in attrs.items() if v not in (None, "")},
            }
            sink.emit(TranscriptEvent(
                TranscriptKind.SUBAGENT_START,
                text=f"Dispatched subagent “{name}”",
                data=data,
            ))
        else:
            data = {
                "lifecycle": "subagent", "status": status, "name": name, "engine": "codex",
                "subagent_key": thread_id, **{k: v for k, v in attrs.items() if v not in (None, "")},
            }
            sink.emit(TranscriptEvent(TranscriptKind.SUBAGENT_END, text=f"{name} {status}", data=data))

    def _emit_child_meta(
        self, sink: TranscriptSink, thread_id: str, attrs: dict[str, object], source: Path,
    ) -> None:
        event = TranscriptEvent(
            TranscriptKind.SESSION_META,
            data={
                "subagent_thread_id": thread_id,
                **{k: v for k, v in attrs.items() if v not in (None, "")},
            },
        )
        self._emit_source_event(sink, event, source, 0, "child-session-meta")

    def _emit_child_event(
        self, sink: TranscriptSink, thread_id: str, attrs: dict[str, object],
        event: TranscriptEvent, source: Path, line_no: int, line: str, event_index: int = 0,
    ) -> None:
        if event.kind is TranscriptKind.SUBAGENT_END:
            event.data["subagent_key"] = thread_id
            event.data["name"] = attrs.get("subagent_type") or attrs.get("agent_nickname") or "subagent"
            event = dataclasses.replace(
                event,
                text=f"{event.data['name']} {event.data.get('status', 'completed')}",
            )
        else:
            event.data["subagent_thread_id"] = thread_id
        event.data.update({k: v for k, v in attrs.items() if v not in (None, "")})
        self._emit_source_event(sink, event, source, line_no, line, event_index)

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
        attrs_for_child = dict(attrs)
        attrs_for_child["subagent_type"] = role or "subagent"
        self._emit_child_meta(sink, thread_id, attrs_for_child, child)
        for line_no, line in enumerate(child.read_text("utf-8", errors="replace").splitlines(), 1):
            for event_index, event in enumerate(parse_codex_rollout_line(line)):
                self._emit_child_event(
                    sink, thread_id, attrs_for_child, event, child, line_no, line, event_index,
                )
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
        if self._watcher is not None:
            self._watcher.stop()
            live_children = self._watcher.child_ids
            self._watcher = None
        else:
            live_children = ()
        parent_id = self._parent_thread_id(transcript)
        if parent_id:
            self.emit_parent_model(parent_id, sink)
        thread_ids = [*self._spawned_thread_ids(transcript), *live_children]
        sessions = self._sessions_dir()
        for thread_id in dict.fromkeys(thread_ids):
            self._ingest_child(sessions, thread_id, sink)

    def _after_stream_line(self, request: HarnessRequest, sink: TranscriptSink, line: str) -> None:
        if request.artifact_dir is None:
            return
        thread_id = codex_session_id(line)
        if thread_id:
            if self._watcher is None:
                self._source_seen = set()
                self._last_compaction_at = {}
                self._watcher = _CodexRolloutWatcher(self, self._sessions_dir(), sink)
            self._watcher.set_parent(thread_id)
