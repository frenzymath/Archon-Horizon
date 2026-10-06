"""Observe native counters and children omitted by some Codex stdout builds.

Only bounded facts from the identified parent rollout enter the durable journal.
Prompts and answers never leave this reader. Counter scope is explicit so stdout
and rollout evidence can share the same accounting checkpoint.
"""

from datetime import datetime
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time
from uuid import UUID, uuid5

from .contracts import Operation

TERMINAL = {"completed", "succeeded", "failed", "errored", "cancelled", "interrupted", "shutdown"}
CALLS = {"list_agents", "wait_agent", "wait", "spawn_agent", "followup_task"}


def terminal_children(payload, calls, parent_path="/root"):
    """Correlate tool results with provider-recorded collaboration calls."""
    kind, call_id = payload.get("type"), payload.get("call_id")
    if kind == "agent_message":
        author, recipient, content = payload.get("author"), payload.get("recipient"), payload.get("content")
        if (not isinstance(author, str) or recipient != parent_path or not author.startswith("/")
                or author.rsplit("/", 1)[0] != recipient or not isinstance(content, list) or not content):
            return []
        if len(author) > 256 or not re.fullmatch(r"/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+", author):
            return []
        first = content[0]
        header = f"Message Type: FINAL_ANSWER\nTask name: {recipient}\nSender: {author}\nPayload:\n"
        if (isinstance(first, dict) and first.get("type") == "input_text"
                and isinstance(first.get("text"), str) and first["text"].startswith(header)):
            return [{"key": author, "status": "completed", "discovered": True}]
        return []
    if kind in ("function_call", "custom_tool_call"):
        name = str(payload.get("name", "")).rsplit(".", 1)[-1]
        if name in CALLS and isinstance(call_id, str):
            calls[call_id] = name
            if name in ("spawn_agent", "followup_task"):
                try:
                    args = payload.get("arguments", payload.get("input", {}))
                    args = json.loads(args) if isinstance(args, str) else args
                    target = args.get("task_name" if name == "spawn_agent" else "target") if isinstance(args, dict) else None
                    if isinstance(target, str) and re.fullmatch(r"[A-Za-z0-9_-]+", target):
                        target = parent_path + "/" + target
                    if isinstance(target, str) and target.startswith("/") and len(target) <= 256:
                        calls[call_id] = {"name": name, "target": target}
                except ValueError:
                    pass
            while len(calls) > 128:
                calls.pop(next(iter(calls)))
        return []
    if kind not in ("function_call_output", "custom_tool_call_output") or call_id not in calls:
        return []
    name = calls.pop(call_id)
    target = name.get("target") if isinstance(name, dict) else None
    name = name["name"] if isinstance(name, dict) else name
    try:
        value = json.loads(payload["output"]) if isinstance(payload.get("output"), str) else payload.get("output")
    except ValueError:
        return []
    if not isinstance(value, dict):
        return []
    if name in ("spawn_agent", "followup_task"):
        native_id = value.get("agent_id")
        key = value.get("agent_name") or target or native_id
        if isinstance(key, str) and 0 < len(key) <= 256:
            return [{"key": key, "status": "running", "discovered": True, "invocation_id": call_id,
                     **({"native_id": native_id} if isinstance(native_id, str) and native_id != key else {})}]
        return []
    states = []
    if name == "list_agents" and isinstance(value.get("agents"), list):
        states = [(agent.get("agent_name"), agent.get("agent_status"))
                  for agent in value["agents"][:128] if isinstance(agent, dict)]
    elif isinstance(value.get("status"), dict):
        states = list(value["status"].items())[:128]
    result = []
    for key, state in states:
        if isinstance(state, dict):
            state = next((candidate for candidate in TERMINAL if candidate in state), None)
        if isinstance(key, str) and 0 < len(key) <= 256 and isinstance(state, str) and state in TERMINAL:
            result.append({"key": key, "status": state})
    return result


class CodexLifecycleObserver:
    def __init__(self, journal, provider_home, *, request_id, execution_id, epoch, thread_record_id, since,
                 native_thread_id=None):
        self.journal = journal
        self.root = Path(provider_home) / ".codex" / "sessions"
        self.request_id, self.execution_id, self.epoch = request_id, execution_id, epoch
        self.thread_record_id, self.since = thread_record_id, since
        self.cursor_path = journal.state_root / "requests" / request_id / "child-lifecycle.json"
        self.cursor = {"offset": 0, "calls": {}}
        try:
            saved = json.loads(self.cursor_path.read_text())
            if (isinstance(saved, dict) and type(saved.get("offset")) is int and saved["offset"] >= 0
                    and isinstance(saved.get("calls"), dict) and len(saved["calls"]) <= 128
                    and all(isinstance(key, str) and (isinstance(value, str) and value in CALLS or
                            isinstance(value, dict) and value.get("name") in ("spawn_agent", "followup_task")
                            and isinstance(value.get("target"), str) and len(value["target"]) <= 256)
                            for key, value in saved["calls"].items())
                    and all(isinstance(saved.get(key), str) for key in ("path", "thread_id"))):
                self.cursor = saved
        except (OSError, ValueError, TypeError):
            pass
        self.next_poll = 0
        # Capture the resume boundary before launching the new provider turn.
        # Historical recovery deliberately omits native_thread_id and scans from zero.
        if native_thread_id and not self.cursor.get("path"):
            try:
                path = self._find(native_thread_id)
                if path:
                    self.cursor["offset"] = path.stat().st_size
                    self._save()
            except OSError:
                pass

    def _find(self, thread_id):
        try:
            UUID(thread_id)
        except (ValueError, TypeError):
            return None
        if self.cursor.get("thread_id") not in (None, thread_id):
            return None
        candidates = self.root.glob("*/*/*/rollout-*-" + thread_id + ".jsonl")
        for path in candidates:
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                continue
            try:
                with path.open("rb") as source:
                    metadata = json.loads(source.readline(1024 * 1024))
            except (OSError, ValueError):
                continue
            if (isinstance(metadata, dict) and metadata.get("type") == "session_meta"
                    and isinstance(metadata.get("payload"), dict) and metadata["payload"].get("id") == thread_id):
                self.cursor.update(path=str(path), thread_id=thread_id)
                self.cursor["provider_version"] = metadata["payload"].get("cli_version")
                source = metadata["payload"].get("source")
                subagent = source.get("subagent") if isinstance(source, dict) else None
                spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
                agent_path = spawn.get("agent_path") if isinstance(spawn, dict) else None
                self.cursor["agent_path"] = agent_path if isinstance(agent_path, str) and len(agent_path) <= 256 else "/root"
                return path
        return None

    def annotate(self, event, thread_id):
        """Corroborate stdout scope with this request's explicit rollout totals."""
        if event.get("type") != "turn.completed" or not thread_id:
            return event
        try:
            self.poll(thread_id, force=True)
        except OSError:
            return event
        totals, usage = self.cursor.get("last_usage"), event.get("usage")
        if self.cursor.get("thread_id") != thread_id or not isinstance(totals, dict) or not isinstance(usage, dict):
            return event
        keys = ("input_tokens", "output_tokens", "cached_input_tokens")
        if any(type(usage.get(key)) is not int or usage[key] != totals.get(key) for key in keys if key in usage):
            return event
        if "input_tokens" not in usage or "output_tokens" not in usage:
            return event
        return {**event, "accounting": self._accounting(thread_id, "rollout_correlated_completion")}

    def _accounting(self, thread_id, source):
        return {"scope": "native_thread_cumulative", "identity": thread_id, "epoch": "session",
                "source": source, "provider_version": self.cursor.get("provider_version")}

    def poll(self, thread_id, *, force=False):
        try:
            return self._poll(thread_id, force=force)
        except OSError:
            # Diagnostics may disappear during retention or have restricted access.
            # Durable journal failures are not swallowed by this filesystem guard.
            return 0

    def _poll(self, thread_id, *, force=False):
        now = time.monotonic()
        if not thread_id or (not force and now < self.next_poll):
            return 0
        self.next_poll = now + 1
        saved = deepcopy(self.cursor)
        path = Path(self.cursor["path"]) if self.cursor.get("path") else self._find(thread_id)
        if path is None or self.cursor.get("thread_id") != thread_id:
            return 0
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            return 0
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return 0
        count, scanned = 0, 0
        with os.fdopen(fd, "rb") as source:
            try:
                metadata = json.loads(source.readline(1024 * 1024))
            except ValueError:
                return 0
            if not isinstance(metadata, dict) or metadata.get("type") != "session_meta" or (
                    not isinstance(metadata.get("payload"), dict) or metadata["payload"].get("id") != thread_id):
                return 0
            stat = os.fstat(source.fileno())
            identity = [stat.st_dev, stat.st_ino]
            if self.cursor.get("identity", identity) != identity or stat.st_size < self.cursor["offset"]:
                return 0
            self.cursor["identity"] = identity
            source.seek(self.cursor["offset"])
            while scanned < 4 * 1024 * 1024:
                start = source.tell()
                line = source.readline(1024 * 1024 + 1)
                if not line:
                    break
                scanned += len(line)
                if self.cursor.get("discard_line") or len(line) > 1024 * 1024:
                    self.cursor.update(offset=source.tell(), discard_line=not line.endswith(b"\n"))
                    continue
                if not line.endswith(b"\n"):
                    break
                calls = dict(self.cursor["calls"])
                try:
                    raw = json.loads(line)
                    at = datetime.fromisoformat(raw["timestamp"].replace("Z", "+00:00")).timestamp()
                    children = terminal_children(raw.get("payload", {}), calls, self.cursor.get("agent_path", "/root")) if raw.get("type") == "response_item" else []
                    payload = raw.get("payload", {})
                    info = payload.get("info") if isinstance(payload, dict) else None
                    totals = (info.get("total_token_usage") if raw.get("type") == "event_msg"
                              and payload.get("type") == "token_count" and isinstance(info, dict) else None)
                except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
                    children, totals, at = [], None, 0
                observation = {"type": "horizon.child_notifications", "children": children} if children else None
                if isinstance(totals, dict):
                    from ..provider_events import select_native_event
                    observation = select_native_event("codex", {"type": "horizon.usage_snapshot", "usage": totals,
                        "accounting": self._accounting(thread_id, "rollout_total_token_usage")})
                if observation and at >= self.since:
                    operation_id = str(uuid5(UUID(self.request_id), "child-lifecycle:" + str(start)))
                    if self.journal.operation(operation_id) is None:
                        self.journal.enqueue(Operation.create(self.execution_id, self.epoch, "provider_observed", {
                            "event": "native_event", "request_id": self.request_id,
                            "provider_thread_record_id": self.thread_record_id, "adapter": "codex",
                            "raw": observation},
                            operation_id=operation_id, occurred_at=at))
                    count += 1
                    if observation["type"] == "horizon.usage_snapshot":
                        self.cursor["last_usage"] = observation["usage"]
                self.cursor.update(offset=source.tell(), calls=calls)
            if self.cursor != saved:
                self._save()
        return count

    def _save(self):
        self.cursor_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.cursor_path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as target:
            json.dump(self.cursor, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, self.cursor_path)
