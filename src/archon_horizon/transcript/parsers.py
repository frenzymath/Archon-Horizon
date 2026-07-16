"""Per-engine native-log -> canonical event parsers.

Each parser maps ONE native output line to zero or more canonical
:class:`TranscriptEvent`s. They are best-effort and defensive: a line that
doesn't match a known shape yields nothing rather than raising, so a CLI
version bump degrades gracefully instead of crashing a run. Adjust the field
mappings here when an engine changes its schema — this is the only place that
knows a native format.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from datetime import datetime
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


def _claude_model(obj: dict) -> str | None:
    """The model an engine reported on one line, regardless of where it lives.

    Claude stamps it on the ``system``/``init`` line (``model``), on every
    ``assistant`` message (``message.model``), and on the final ``result``
    (``modelUsage`` keyed by model id). We read whichever is present, so the real
    model is captured even when the config never pinned ``--model``.
    """
    model = obj.get("model")
    if isinstance(model, str) and model:
        return model
    message = obj.get("message")
    if isinstance(message, dict):
        nested = message.get("model")
        if isinstance(nested, str) and nested:
            return nested
    model_usage = obj.get("modelUsage") or obj.get("model_usage")
    if isinstance(model_usage, dict) and model_usage:
        def _tokens(entry: object) -> int:
            if isinstance(entry, dict):
                return _int_or_zero(entry.get("inputTokens") or entry.get("input_tokens")) + _int_or_zero(
                    entry.get("outputTokens") or entry.get("output_tokens")
                )
            return 0
        # The busiest model is the one that did the work (ignore tiny haiku
        # title-generation calls claude bills under a second model id).
        return max(model_usage, key=lambda key: _tokens(model_usage[key]))
    return None


def observed_model(events: list[TranscriptEvent]) -> str | None:
    """The model the engine actually used, scanned from canonical events — the
    parsers stamp it onto the session-meta and usage events. ``None`` when no
    engine reported one (e.g. the null harness)."""
    for event in events:
        model = event.data.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def observed_effort(events: list[TranscriptEvent]) -> str | None:
    """The reasoning-effort tier the engine actually ran with, scanned from
    canonical events — the codex rollout parser stamps it onto session-meta from
    the engine's own ``turn_context``. ``None`` when the engine reported none
    (e.g. Claude Code, which takes effort as an input ``--effort`` flag it does
    not echo back in its stream; the run view then falls back to the configured
    tier)."""
    for event in events:
        effort = event.data.get("effort")
        if isinstance(effort, str) and effort:
            return effort
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
    """Claude Code ``--output-format stream-json`` lines.

    A subagent's interior events arrive **inline** in this same stream, each
    tagged with ``parent_tool_use_id`` (the ``tool_use`` id of the Task/Agent call
    that spawned it) and ``subagent_type``. We stamp those onto each event's
    ``data`` so the run view can attribute / nest subagent activity.
    """
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
    elif kind == "system":
        # The init line announces the model claude resolved (even when no
        # --model was passed); carry it on a session_meta event so the run view
        # can show the real model. session_meta events aren't rendered as rows.
        model = _claude_model(obj)
        if model:
            events.append(TranscriptEvent(TranscriptKind.SESSION_META, data={"model": model}))
    elif kind == "result":
        usage = line_usage or TranscriptUsage()
        data = _usage_data(usage)
        model = _claude_model(obj)
        if model:
            data["model"] = model
        events.append(TranscriptEvent(TranscriptKind.USAGE, data=data, usage=usage))
        subtype = str(obj.get("subtype") or "")
        if obj.get("is_error") or subtype.startswith("error"):
            # The engine's own structured verdict on the run — surfaced as an
            # ERROR event so failure classification can read the typed subtype
            # (e.g. ``error_during_execution``) instead of grepping stderr.
            events.append(TranscriptEvent(
                TranscriptKind.ERROR,
                text=str(obj.get("result") or subtype or "engine reported an error"),
                data={"subtype": subtype} if subtype else {},
            ))
        if obj.get("result"):
            events.append(TranscriptEvent(TranscriptKind.TEXT, text=obj["result"]))
    parent = obj.get("parent_tool_use_id")
    if parent:
        attr: dict[str, object] = {"parent_tool_use_id": parent}
        subagent = obj.get("subagent_type")
        if subagent:
            attr["subagent_type"] = subagent
        # Stamp the subagent's own model (from its assistant `message.model`) so
        # the run view shows it per-subagent — it can differ from the parent's
        # (e.g. a cheaper model delegated to a read-only subagent).
        model = _claude_model(obj)
        if model:
            attr["model"] = model
        for event in events:
            event.data.update(attr)
    return events


def parse_codex_line(line: str) -> list[TranscriptEvent]:
    """Codex ``exec --json`` thread/turn/item events.

    Codex emits each item three times — ``item.started``, ``item.updated``,
    ``item.completed`` — so we key off the terminal ``item.completed`` only;
    parsing every ``item.*`` duplicated every command and message in the log.
    A finished command carries both its invocation and its output, which we
    split into a ``tool_call`` + a ``tool_result`` so the log shows what ran
    *and* what it printed (the old parser dropped the output entirely).
    """
    obj = _loads(line)
    if obj is None:
        return []
    kind = obj.get("type", "")
    line_usage = _usage_from_native(obj)
    if kind == "item.completed":
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
        if itype in ("command_execution", "tool_call"):
            # Unify labels with the claude transcript: a shell command reads as
            # "Bash" (not "command_execution"); a real tool keeps its own name.
            tool = "Bash" if itype == "command_execution" else str(item.get("name") or "tool_call")
            call = TranscriptEvent(
                TranscriptKind.TOOL_CALL,
                tool=tool,
                data={"command": item.get("command")},
                usage=event_usage,
            )
            output = item.get("aggregated_output") or item.get("output") or item.get("stdout")
            if output:
                return [
                    call,
                    TranscriptEvent(
                        TranscriptKind.TOOL_RESULT,
                        tool=tool,
                        data={"content": output, "exit_code": item.get("exit_code")},
                    ),
                ]
            return [call]
        if itype == "file_change":
            # Reads as "Edit" with the changed paths, not an empty "file_change".
            return [
                TranscriptEvent(
                    TranscriptKind.TOOL_CALL,
                    tool="Edit",
                    data={"changes": item.get("changes")},
                    usage=event_usage,
                )
            ]
        if itype == "collab_tool_call":
            # Subagent delegation: ``spawn_agent`` / ``wait`` carrying the child
            # thread id(s). We surface it as a tool call and keep the receiver
            # thread ids so the harness can ingest each child's separate rollout.
            receivers = item.get("receiver_thread_ids") or item.get("receiver_thread_id")
            if isinstance(receivers, str):
                receivers = [receivers]
            return [
                TranscriptEvent(
                    TranscriptKind.TOOL_CALL,
                    tool=str(item.get("tool") or "spawn_agent"),
                    data={
                        "sender_thread_id": item.get("sender_thread_id"),
                        "receiver_thread_ids": list(receivers or []),
                    },
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


_CODEX_SHELL_TOOLS = ("exec_command", "shell", "local_shell", "bash", "container.exec")


def _codex_ts(obj: dict) -> datetime | None:
    """The native ISO timestamp on a Codex line, as an aware datetime — so events
    keep their real order/time instead of defaulting to parse time."""
    raw = obj.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _codex_command_str(args: object) -> str:
    """Pull a clean command string out of a Codex shell call's decoded arguments
    (``{"cmd": [...]}`` / ``{"command": "..."}``), mirroring the main stream so a
    subagent's Bash call reads as a command, not raw JSON."""
    if isinstance(args, dict):
        cmd = args.get("command", args.get("cmd"))
    else:
        cmd = args
    if isinstance(cmd, list):
        return " ".join(str(part) for part in cmd)
    return "" if cmd is None else str(cmd)


def _decode_codex_args(raw: object) -> object:
    """Codex tool arguments arrive as a JSON *string*; decode to an object so the
    UI renders structure instead of an escaped blob. Returns the raw value if it
    is not decodable JSON."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _codex_rollout_item(payload: dict) -> list[TranscriptEvent]:
    ptype = payload.get("type")
    if ptype == "message":
        # Only the agent's own narration — skip injected developer/user prompts.
        out: list[TranscriptEvent] = []
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text") and block.get("text"):
                out.append(TranscriptEvent(TranscriptKind.TEXT, text=block["text"]))
        return out
    if ptype == "reasoning":
        parts = [
            s["text"] if isinstance(s, dict) else s
            for s in payload.get("summary") or []
            if (isinstance(s, dict) and s.get("text")) or isinstance(s, str)
        ]
        text = "\n".join(parts).strip()
        return [TranscriptEvent(TranscriptKind.THINKING, text=text)] if text else []
    if ptype in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "tool_call")
        raw = payload.get("arguments") if ptype == "function_call" else payload.get("input")
        args = _decode_codex_args(raw)
        if name in _CODEX_SHELL_TOOLS:
            # Same shape as the main stream's command_execution, so the UI's
            # ShellCommandBlock renders it instead of dumping raw JSON.
            return [TranscriptEvent(TranscriptKind.TOOL_CALL, tool="Bash", data={"command": _codex_command_str(args)})]
        return [TranscriptEvent(TranscriptKind.TOOL_CALL, tool=name, data={"input": args})]
    if ptype in ("function_call_output", "custom_tool_call_output"):
        return [TranscriptEvent(TranscriptKind.TOOL_RESULT, data={"content": payload.get("output")})]
    return []


def parse_codex_rollout_line(line: str) -> list[TranscriptEvent]:
    """One line of a Codex ``rollout-*.jsonl`` session file.

    Codex writes each thread's full interior to its own rollout file under
    ``$CODEX_HOME/sessions/``. A spawned subagent runs in a *separate* thread, so
    its activity lives only in that file (not the parent's ``exec --json`` stream)
    — this parser folds it back into the run transcript. Lines are
    ``{type, payload}``; the conversational substance is in ``response_item``
    payloads, with token usage in ``event_msg``/``token_count``.
    """
    obj = _loads(line)
    if obj is None:
        return []
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    at = _codex_ts(obj)
    otype = obj.get("type")
    events: list[TranscriptEvent] = []
    if otype == "response_item":
        events = _codex_rollout_item(payload)
    elif otype in ("session_meta", "turn_context"):
        # Codex records its model, reasoning effort, and the spawned subagent's
        # role/nickname here; surface them so the run view shows the real model +
        # effort (verified from the engine's own stream, not just config) and a
        # friendly subagent name.
        model = payload.get("model")
        meta: dict[str, object] = {}
        if isinstance(model, str) and model:
            meta["model"] = model
        # Effort key has been renamed across codex versions (like agent_role), so
        # try each candidate; the value is a tier string ("high"/"xhigh"/…).
        for ekey in ("effort", "reasoning_effort", "model_reasoning_effort"):
            val = payload.get(ekey)
            if isinstance(val, str) and val:
                meta["effort"] = val
                break
        for key in ("agent_role", "agent_nickname"):
            val = payload.get(key)
            if isinstance(val, str) and val:
                meta[key] = val
        if meta:
            events = [TranscriptEvent(TranscriptKind.SESSION_META, data=meta)]
    elif otype == "event_msg" and payload.get("type") == "token_count":
        info = payload.get("info") or {}
        last = info.get("last_token_usage") or info.get("total_token_usage") or {}
        usage = TranscriptUsage(
            tokens_in=int(last.get("input_tokens", 0) or 0),
            tokens_out=int(last.get("output_tokens", 0) or 0),
            cached_tokens_in=int(last.get("cached_input_tokens", 0) or 0),
            reasoning_tokens_out=int(last.get("reasoning_output_tokens", 0) or 0),
        )
        events = [TranscriptEvent(TranscriptKind.USAGE, data=_usage_data(usage), usage=usage)]
    if at is not None:
        events = [dataclasses.replace(event, at=at) for event in events]
    return events


# ── native session-id extraction (for --resume) ──────────────────────
# Each engine stamps its own session/thread id on its stream; we capture the
# first one seen so it can be stored in the session meta.json (and replayed via
# the engine's native resume flag). Pure per-line, like the parsers.


def claude_session_id(line: str) -> str | None:
    """Claude Code puts ``session_id`` on every stream-json event."""
    obj = _loads(line)
    if obj is None:
        return None
    sid = obj.get("session_id")
    return sid if isinstance(sid, str) and sid else None


def codex_session_id(line: str) -> str | None:
    """Codex announces its thread id on the opening ``thread.started`` event."""
    obj = _loads(line)
    if obj is None:
        return None
    if obj.get("type") in ("thread.started", "thread.created", "session.created"):
        for key in ("thread_id", "session_id", "id"):
            sid = obj.get(key)
            if isinstance(sid, str) and sid:
                return sid
    thread = obj.get("thread")
    if isinstance(thread, dict):
        sid = thread.get("id") or thread.get("thread_id")
        if isinstance(sid, str) and sid:
            return sid
    return None


def aggregate(events: list[TranscriptEvent]) -> tuple[str, Usage]:
    """Reduce a transcript to (final_text, usage) for the HarnessResult.

    ``final_text`` is the agent's *last* text message — its closing summary — not
    every text event joined. Engines emit one text event per assistant turn
    (the narration between tool calls), so joining them all produced a report
    that was the whole running commentary repeated, instead of the final report
    the agent signs off with.
    """
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
    return (texts[-1] if texts else ""), usage
