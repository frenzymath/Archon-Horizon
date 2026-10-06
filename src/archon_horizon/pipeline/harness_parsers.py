"""Per-engine native-log -> canonical event parsers.

Each parser maps ONE native output line to zero or more canonical
:class:`HarnessEvent`s. They are best-effort and defensive: a line that
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
from .harness_events import HarnessEvent, HarnessEventKind, HarnessUsage

HarnessParser = Callable[[str], list[HarnessEvent]]


def parse_plain_line(line: str) -> list[HarnessEvent]:
    """Treat raw stdout as plain text, one event per non-empty line."""
    return [HarnessEvent(HarnessEventKind.TEXT, text=line)] if line.strip() else []


def _loads(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _native_ts(obj: dict) -> datetime | None:
    """The native ISO timestamp shared by Claude and Codex persisted logs."""
    raw = obj.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tag_value(text: str, tag: str) -> str | None:
    """Read one simple XML-like field from Claude's task notification."""
    opening, closing = f"<{tag}>", f"</{tag}>"
    start = text.find(opening)
    if start < 0:
        return None
    start += len(opening)
    end = text.find(closing, start)
    return text[start:end].strip() if end >= 0 else None


def _subagent_name(args: object, fallback: str = "subagent") -> str:
    if isinstance(args, dict):
        for key in ("description", "task_name", "agent_type", "name", "subagent_type"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback


def _subagent_description(args: object) -> str | None:
    """Return the human task description without retaining the full prompt."""
    if isinstance(args, dict):
        for key in ("description", "message", "prompt", "task_name"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:240]
    return None


def _subagent_start(*, name: str, key: str | None = None, engine: str, attrs: dict | None = None) -> HarnessEvent:
    data: dict[str, object] = {"lifecycle": "subagent", "status": "running", "name": name, "engine": engine}
    if key:
        data["subagent_key"] = key
    if attrs:
        data.update({k: v for k, v in attrs.items() if v is not None and v != ""})
    return HarnessEvent(HarnessEventKind.SUBAGENT_START, text=f"Dispatched subagent “{name}”", data=data)


def _subagent_end(
    *, name: str | None = None, key: str | None = None, status: str = "completed",
    engine: str, summary: str | None = None, attrs: dict | None = None,
) -> HarnessEvent:
    label = name.strip() if isinstance(name, str) and name.strip() else "Subagent"
    data: dict[str, object] = {"lifecycle": "subagent", "status": status, "engine": engine}
    if name:
        data["name"] = name
    if key:
        data["subagent_key"] = key
    if summary:
        data["summary"] = summary
    if attrs:
        data.update({k: v for k, v in attrs.items() if v is not None and v != ""})
    return HarnessEvent(HarnessEventKind.SUBAGENT_END, text=f"{label} {status}", data=data)


def _workflow_progress(
    *, name: str | None = None, status: str = "running", attrs: dict | None = None,
) -> HarnessEvent:
    label = name.strip() if isinstance(name, str) and name.strip() else "Workflow"
    data: dict[str, object] = {
        "lifecycle": "workflow",
        "status": status,
        "name": label,
        "engine": "claude",
    }
    if attrs:
        data.update({k: v for k, v in attrs.items() if v is not None and v != ""})
    done = _int_or_zero(data.get("completed_agents"))
    total = _int_or_zero(data.get("total_agents"))
    progress = f": {done}/{total} agents done" if total else " started"
    return HarnessEvent(
        HarnessEventKind.WORKFLOW_PROGRESS,
        text=f'{label}{progress}',
        data=data,
    )


def _notification_text(obj: dict) -> str | None:
    """Return Claude's task notification across its CLI persistence shapes."""
    content = obj.get("content")
    if isinstance(content, str):
        return content
    message = obj.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    attachment = obj.get("attachment")
    if isinstance(attachment, dict) and isinstance(attachment.get("prompt"), str):
        return attachment["prompt"]
    return None


def _claude_workflow_events(obj: dict) -> list[HarnessEvent]:
    """Read Workflow launch/completion metadata stored beside Claude messages.

    ``Workflow`` itself is a tool call, but its useful identity is returned on
    the following user row under ``toolUseResult``. Completion is delivered as
    a task notification and includes the exact aggregate usage counters.
    """
    result = obj.get("toolUseResult")
    if isinstance(result, dict) and result.get("taskType") == "local_workflow":
        status = str(result.get("status") or "running")
        return [_workflow_progress(
            name=str(result.get("workflowName") or "Workflow"),
            status="running" if status == "async_launched" else status,
            attrs={
                "workflow_id": result.get("runId"),
                "run_id": result.get("runId"),
                "task_id": result.get("taskId"),
                "summary": result.get("summary"),
                "transcript_dir": result.get("transcriptDir"),
                "script_path": result.get("scriptPath"),
                "completed_agents": 0,
            },
        )]

    content = _notification_text(obj)
    if not content or "<task-notification>" not in content:
        return []
    total = _tag_value(content, "agent_count")
    done = _tag_value(content, "agents_done")
    summary = _tag_value(content, "summary")
    # Ordinary background commands and single Agent tasks use the same envelope.
    if total is None and not (summary and summary.startswith("Dynamic workflow ")):
        return []
    duration_ms = _int_or_zero(_tag_value(content, "duration_ms"))
    attrs: dict[str, object] = {
        "task_id": _tag_value(content, "task-id"),
        "tool_use_id": _tag_value(content, "tool-use-id"),
        "summary": summary,
        "completed_agents": _int_or_zero(done),
        "total_agents": _int_or_zero(total),
        "agents_error": _int_or_zero(_tag_value(content, "agents_error")),
        "agents_skipped": _int_or_zero(_tag_value(content, "agents_skipped")),
        "empty_results": _int_or_zero(_tag_value(content, "agents_empty_result")),
        "subagent_tokens": _int_or_zero(_tag_value(content, "subagent_tokens")),
        "tool_uses": _int_or_zero(_tag_value(content, "tool_uses")),
        "duration_seconds": duration_ms / 1000 if duration_ms else None,
    }
    return [_workflow_progress(
        status=(_tag_value(content, "status") or "completed").lower(),
        attrs=attrs,
    )]


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


def _usage_from_native(data: dict) -> HarnessUsage | None:
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
        return HarnessUsage(
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


# Event data keys that mark an event as belonging to a nested subagent thread
# (or its dispatch), not the parent session. Parent model/effort readback must
# ignore these — otherwise the first spawned child's model (e.g. luna) is shown
# as the main session's model (e.g. gpt-5.6-sol).
_SUBAGENT_EVENT_KEYS = (
    "subagent_thread_id",
    "subagent_type",
    "subagent_id",
    "subagent_key",
    "parent_tool_use_id",
    "native_subagent_id",
)


def _is_subagent_scoped_event(event: HarnessEvent) -> bool:
    """True when ``event`` is about a nested subagent, not the parent session."""
    data = event.data or {}
    if event.kind in (HarnessEventKind.SUBAGENT_START, HarnessEventKind.SUBAGENT_END):
        return True
    return any(data.get(key) not in (None, "") for key in _SUBAGENT_EVENT_KEYS)


def observed_model(events: list[HarnessEvent]) -> str | None:
    """The model the **parent** session actually used, from canonical events.

    Parsers stamp model onto session-meta and usage events. Nested subagent
    events also carry a ``model`` (often a different, cheaper one) — those are
    skipped so the run view does not mislabel the main agent. ``None`` when no
    parent engine reported one (e.g. the null harness).
    """
    for event in events:
        if _is_subagent_scoped_event(event):
            continue
        model = event.data.get("model")
        if isinstance(model, str) and model:
            return model
    return None


def observed_effort(events: list[HarnessEvent]) -> str | None:
    """The reasoning-effort tier the **parent** session actually ran with.

    Scanned from canonical events — the codex rollout parser stamps it onto
    session-meta from the engine's own ``turn_context``. Subagent-scoped events
    are ignored (same reason as :func:`observed_model`). ``None`` when the
    engine reported none (e.g. Claude Code, which takes effort as an input
    ``--effort`` flag it does not echo back; the run view then falls back to
    the configured tier).
    """
    for event in events:
        if _is_subagent_scoped_event(event):
            continue
        effort = event.data.get("effort")
        if isinstance(effort, str) and effort:
            return effort
    return None


def _usage_data(usage: HarnessUsage | None) -> dict[str, object]:
    if usage is None:
        return {"tokens_in": 0, "tokens_out": 0, "cost_usd": None}
    return {
        "tokens_in": usage.tokens_in,
        "tokens_out": usage.tokens_out,
        "cached_tokens_in": usage.cached_tokens_in,
        "reasoning_tokens_out": usage.reasoning_tokens_out,
        "cost_usd": usage.cost_usd,
    }


def _codex_context_data(info: dict, last: dict, total: dict) -> dict[str, object]:
    """Normalize Codex's current/cumulative token snapshot without conflating it
    with the per-turn ``HarnessUsage`` used for billing aggregation."""
    def number(source: dict, *keys: str) -> int:
        for key in keys:
            value = source.get(key)
            if value is not None:
                return _int_or_zero(value)
        return 0

    window = number(
        info,
        "model_context_window", "context_window", "model_context_window_tokens",
    )
    return {
        "request_tokens_in": number(last, "input_tokens", "tokens_in", "prompt_tokens"),
        "request_tokens_out": number(last, "output_tokens", "tokens_out", "completion_tokens"),
        "request_cached_tokens_in": number(last, "cached_input_tokens", "cached_tokens_in"),
        "cumulative_tokens_in": number(total, "input_tokens", "tokens_in", "prompt_tokens"),
        "cumulative_tokens_out": number(total, "output_tokens", "tokens_out", "completion_tokens"),
        "cumulative_cached_tokens_in": number(total, "cached_input_tokens", "cached_tokens_in"),
        "model_context_window": window,
    }


def parse_claude_line(line: str) -> list[HarnessEvent]:
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
    events: list[HarnessEvent] = []
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
                    HarnessEvent(HarnessEventKind.TEXT, text=block.get("text", ""), usage=event_usage)
                )
            elif btype == "thinking":
                events.append(
                    HarnessEvent(
                        HarnessEventKind.THINKING,
                        text=block.get("thinking", ""),
                        usage=event_usage,
                    )
                )
            elif btype == "tool_use":
                events.append(
                    HarnessEvent(
                        HarnessEventKind.TOOL_CALL,
                        tool=block.get("name", ""),
                        data={"input": block.get("input", {}), "call_id": block.get("id")},
                        usage=event_usage,
                    )
                )
                # Claude Code's interactive log keeps child interiors in a
                # separate subagents/ directory, but the parent Agent/Task call
                # is the authoritative instant at which delegation happened.
                # Emit a small parent-log lifecycle row without copying the
                # (potentially huge) prompt into it.
                tool_name = str(block.get("name") or "")
                if tool_name in ("Agent", "Task"):
                    args = block.get("input", {})
                    attrs = {
                        "subagent_type": args.get("subagent_type") if isinstance(args, dict) else None,
                        "model": args.get("model") if isinstance(args, dict) else None,
                        "description": _subagent_description(args),
                        "background": bool(args.get("run_in_background")) if isinstance(args, dict) else False,
                    }
                    events.append(_subagent_start(
                        name=_subagent_name(args),
                        key=str(block.get("id") or "") or None,
                        engine="claude",
                        attrs=attrs,
                    ))
            elif btype == "tool_result":
                events.append(
                    HarnessEvent(
                        HarnessEventKind.TOOL_RESULT,
                        data={"content": block.get("content"), "call_id": block.get("tool_use_id"), "is_error": bool(block.get("is_error"))},
                        usage=event_usage,
                    )
                )
    elif kind == "system":
        # The init line announces the model claude resolved (even when no
        # --model was passed); carry it on a session_meta event so the run view
        # can show the real model. session_meta events aren't rendered as rows.
        model = _claude_model(obj)
        if model:
            events.append(HarnessEvent(HarnessEventKind.SESSION_META, data={"model": model}))
    elif kind == "result":
        usage = line_usage or HarnessUsage()
        data = _usage_data(usage)
        # Claude's `total_cost_usd` is a running session total, while its usage
        # token fields describe this result. Consumers must delta the cost rather
        # than add every snapshot ($532 + $551 + ... is not real spend).
        if obj.get("total_cost_usd") is not None and obj.get("cost_usd") is None:
            data["cost_cumulative"] = True
        model = _claude_model(obj)
        if model:
            data["model"] = model
        events.append(HarnessEvent(HarnessEventKind.USAGE, data=data, usage=usage))
        subtype = str(obj.get("subtype") or "")
        if obj.get("is_error") or subtype.startswith("error"):
            # The engine's own structured verdict on the run — surfaced as an
            # ERROR event so failure classification can read the typed subtype
            # (e.g. ``error_during_execution``) instead of grepping stderr.
            events.append(HarnessEvent(
                HarnessEventKind.ERROR,
                text=str(obj.get("result") or subtype or "engine reported an error"),
                data={"subtype": subtype} if subtype else {},
            ))
        if obj.get("result"):
            events.append(HarnessEvent(HarnessEventKind.TEXT, text=obj["result"]))
    elif kind == "queue-operation" and obj.get("operation") == "enqueue":
        # Async Claude agents announce every stop through a task-notification.
        # The notification contains the spawning tool-use id, a terminal status,
        # and a semantic summary such as `Agent "Lane F1" finished`.  Keep only
        # that compact metadata; the full result remains in Claude's native log.
        content = obj.get("content")
        if isinstance(content, str) and "<task-notification>" in content:
            status = (_tag_value(content, "status") or "completed").lower()
            summary = _tag_value(content, "summary")
            name: str | None = None
            if summary and summary.startswith('Agent "') and summary.endswith('" finished'):
                name = summary[len('Agent "'):-len('" finished')]
            # The same queue also carries background Bash and monitor notices;
            # those are subprocess lifecycle, not agent lifecycle.
            if name:
                events.append(_subagent_end(
                    name=name,
                    key=_tag_value(content, "tool-use-id"),
                    status=status,
                    engine="claude",
                    summary=summary,
                ))
    events.extend(_claude_workflow_events(obj))
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
    at = _native_ts(obj)
    if at is not None:
        events = [dataclasses.replace(event, at=at) for event in events]
    return events


def parse_codex_line(line: str) -> list[HarnessEvent]:
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
            return [HarnessEvent(HarnessEventKind.TEXT, text=item.get("text", ""), usage=event_usage)]
        if itype == "reasoning":
            return [
                HarnessEvent(HarnessEventKind.THINKING, text=item.get("text", ""), usage=event_usage)
            ]
        if itype == "mcp_tool_call":
            arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
            return [HarnessEvent(HarnessEventKind.TOOL_CALL,
                tool=f"mcp__{item.get('server', '')}__{item.get('tool', '')}",
                data={"mcp_server": item.get("server"), "mcp_tool": item.get("tool"),
                      "call_id": item.get("id"), "status": item.get("status"),
                      "arguments": arguments}, usage=event_usage)]
        if itype in ("command_execution", "tool_call"):
            # Unify labels with the claude transcript: a shell command reads as
            # "Bash" (not "command_execution"); a real tool keeps its own name.
            tool = "Bash" if itype == "command_execution" else str(item.get("name") or "tool_call")
            call = HarnessEvent(
                HarnessEventKind.TOOL_CALL,
                tool=tool,
                data={"command": item.get("command")},
                usage=event_usage,
            )
            output = item.get("aggregated_output") or item.get("output") or item.get("stdout")
            if output:
                return [
                    call,
                    HarnessEvent(
                        HarnessEventKind.TOOL_RESULT,
                        tool=tool,
                        data={"content": output, "exit_code": item.get("exit_code")},
                    ),
                ]
            return [call]
        if itype == "file_change":
            # Reads as "Edit" with the changed paths, not an empty "file_change".
            return [
                HarnessEvent(
                    HarnessEventKind.TOOL_CALL,
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
            tool = str(item.get("tool") or "spawn_agent")
            events = [
                HarnessEvent(
                    HarnessEventKind.TOOL_CALL,
                    tool=tool,
                    data={
                        "sender_thread_id": item.get("sender_thread_id"),
                        "receiver_thread_ids": list(receivers or []),
                    },
                    usage=event_usage,
                )
            ]
            if tool == "spawn_agent":
                name = str(item.get("task_name") or item.get("agent_type") or item.get("agent_nickname") or "Codex subagent")
                description = _subagent_description(item)
                for receiver in receivers or [None]:
                    events.append(_subagent_start(
                        name=name,
                        key=receiver if isinstance(receiver, str) else None,
                        engine="codex",
                        attrs={"model": item.get("model"), "effort": item.get("reasoning_effort"), "description": description},
                    ))
            states = item.get("agents_states") or {}
            if isinstance(states, dict):
                for receiver, state in states.items():
                    if not isinstance(state, dict):
                        continue
                    status = state.get("status")
                    if isinstance(status, dict):
                        status = next(iter(status), "running")
                    attrs = {key: state[key] for key in ("model", "effort", "usage") if key in state}
                    if status in {"completed", "succeeded", "failed", "errored", "cancelled", "interrupted", "shutdown"}:
                        message = state.get("message")
                        summary = str(message).strip()[:240] if isinstance(message, str) and message.strip() else None
                        events.append(_subagent_end(key=receiver, status="cancelled" if status == "shutdown" else status,
                            name=state.get("agent_nickname"), engine="codex", summary=summary, attrs=attrs))
                    elif receiver not in (receivers or []) or tool != "spawn_agent":
                        events.append(_subagent_start(key=receiver, name=state.get("agent_nickname") or "Subagent", engine="codex", attrs=attrs))
            at = _native_ts(obj)
            return [dataclasses.replace(event, at=at) for event in events] if at else events
    elif kind == "turn.completed":
        usage = line_usage or HarnessUsage()
        return [
            HarnessEvent(
                HarnessEventKind.USAGE,
                data=_usage_data(usage),
                usage=usage,
            )
        ]
    return []


_CODEX_SHELL_TOOLS = ("exec_command", "shell", "local_shell", "bash", "container.exec")


def _codex_ts(obj: dict) -> datetime | None:
    """The native ISO timestamp on a Codex line, as an aware datetime — so events
    keep their real order/time instead of defaulting to parse time."""
    return _native_ts(obj)


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


def _codex_rollout_item(payload: dict) -> list[HarnessEvent]:
    ptype = payload.get("type")
    if ptype == "message":
        # Only the agent's own narration — skip injected developer/user prompts.
        out: list[HarnessEvent] = []
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") in ("output_text", "text") and block.get("text"):
                out.append(HarnessEvent(HarnessEventKind.TEXT, text=block["text"]))
        return out
    if ptype == "reasoning":
        parts = [
            s["text"] if isinstance(s, dict) else s
            for s in payload.get("summary") or []
            if (isinstance(s, dict) and s.get("text")) or isinstance(s, str)
        ]
        text = "\n".join(parts).strip()
        return [HarnessEvent(HarnessEventKind.THINKING, text=text)] if text else []
    if ptype in ("function_call", "custom_tool_call"):
        name = str(payload.get("name") or "tool_call")
        raw = payload.get("arguments") if ptype == "function_call" else payload.get("input")
        args = _decode_codex_args(raw)
        if name in _CODEX_SHELL_TOOLS:
            # Same shape as the main stream's command_execution, so the UI's
            # ShellCommandBlock renders it instead of dumping raw JSON.
            return [HarnessEvent(HarnessEventKind.TOOL_CALL, tool="Bash", data={"command": _codex_command_str(args)})]
        events = [HarnessEvent(HarnessEventKind.TOOL_CALL, tool=name, data={"input": args, "call_id": payload.get("call_id")})]
        return events
    if ptype in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        events = [HarnessEvent(HarnessEventKind.TOOL_RESULT, data={"content": output})]
        # Older and headless Codex collaboration APIs return terminal agent
        # states from wait_agent as {"status": {id: {"completed": report}}}.
        # Recognize this shape when it appears in the configured output stream.
        decoded = _decode_codex_args(output)
        if isinstance(decoded, dict) and isinstance(decoded.get("agent_id"), str):
            events.append(_subagent_start(name="Subagent", key=decoded["agent_id"], engine="codex",
                attrs={"call_id": payload.get("call_id"), "from_tool_result": True}))
        statuses = decoded.get("status") if isinstance(decoded, dict) else None
        if isinstance(statuses, dict):
            for key, state in statuses.items():
                if not isinstance(state, dict):
                    continue
                for status in ("completed", "failed", "cancelled", "interrupted"):
                    if status not in state:
                        continue
                    report = state.get(status)
                    summary = str(report).strip().splitlines()[0][:240] if report else None
                    events.append(_subagent_end(
                        key=str(key), status=status, engine="codex", summary=summary,
                    ))
                    break
        return events
    return []


def parse_codex_rollout_line(line: str) -> list[HarnessEvent]:
    """One line of a Codex ``rollout-*.jsonl`` session file.

    Parse this shape when a configured harness emits it or the Activity collector
    tails a rollout belonging to one of the worker's identified threads.
    Lines contain ``{type, payload}``, with conversational events in
    ``response_item`` and usage in ``event_msg``/``token_count``.
    """
    obj = _loads(line)
    if obj is None:
        return []
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    at = _codex_ts(obj)
    otype = obj.get("type")
    events: list[HarnessEvent] = []
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
            events = [HarnessEvent(HarnessEventKind.SESSION_META, data=meta)]
    elif otype == "event_msg":
        if payload.get("type") == "sub_agent_activity":
            key = payload.get("agent_thread_id")
            if isinstance(key, str) and key:
                attrs = {"agent_path": payload.get("agent_path"), "description": _subagent_description({
                    key: payload.get(key) for key in ("description", "task_name")})}
                name = str(payload.get("agent_path") or "Subagent")
                status = payload.get("kind")
                if status in {"started", "interacted"}:
                    events = [_subagent_start(key=key, name=name, engine="codex", attrs=attrs)]
                elif status in {"completed", "interrupted"}:
                    events = [_subagent_end(key=key, name=name, status=status, engine="codex", attrs=attrs)]
        elif payload.get("type") == "collab_agent_spawn_end":
            key = payload.get("new_thread_id")
            if isinstance(key, str) and key:
                events = [_subagent_start(key=key,
                    name=payload.get("new_agent_nickname") or payload.get("new_agent_role") or "Subagent",
                    engine="codex", attrs={"model": payload.get("model"), "effort": payload.get("reasoning_effort"),
                        "call_id": payload.get("call_id"), "description": _subagent_description({
                            key: payload.get(key) for key in ("description", "task_name")})})]
        elif payload.get("type") == "collab_waiting_end":
            statuses = payload.get("statuses")
            if isinstance(statuses, dict):
                for key, status in statuses.items():
                    if isinstance(status, dict):
                        status = next(iter(status), "running")
                    if status in {"completed", "errored", "interrupted", "shutdown"}:
                        events.append(_subagent_end(key=key, status="cancelled" if status == "shutdown" else status, engine="codex"))
        elif payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or info.get("total_token_usage") or {}
            total = info.get("total_token_usage") or last
            usage = HarnessUsage(
                tokens_in=int(last.get("input_tokens", 0) or 0),
                tokens_out=int(last.get("output_tokens", 0) or 0),
                cached_tokens_in=int(last.get("cached_input_tokens", 0) or 0),
                reasoning_tokens_out=int(last.get("reasoning_output_tokens", 0) or 0),
            )
            data = _usage_data(usage)
            data.update(_codex_context_data(info, last, total))
            events = [HarnessEvent(HarnessEventKind.USAGE, data=data, usage=usage)]
        elif payload.get("type") == "context_compacted":
            info = payload.get("info") or payload
            if not isinstance(info, dict):
                info = {}
            data = {
                "trigger": info.get("trigger") or info.get("reason") or "automatic",
                "tokens_before": _int_or_zero(
                    info.get("tokens_before") or info.get("input_tokens_before")
                ),
                "tokens_after": _int_or_zero(
                    info.get("tokens_after") or info.get("input_tokens_after")
                ),
                "model_context_window": _int_or_zero(
                    info.get("model_context_window") or info.get("context_window")
                ),
            }
            events = [HarnessEvent(
                HarnessEventKind.COMPACTION,
                text="Context compacted",
                data={key: value for key, value in data.items() if value not in (None, "")},
            )]
        elif payload.get("type") == "task_complete":
            # With fork_turns/all, Codex replays earlier task_complete records at
            # the child rollout's start timestamp.  `completed_at` retains their
            # real epoch, so ignore a replay whose two clocks disagree.
            completed_at = payload.get("completed_at")
            replayed = (
                at is not None
                and isinstance(completed_at, (int, float))
                and abs(at.timestamp() - float(completed_at)) > 5
            )
            if not replayed:
                attrs = {}
                duration_ms = payload.get("duration_ms")
                if isinstance(duration_ms, (int, float)):
                    attrs["duration_seconds"] = max(0, float(duration_ms) / 1000)
                message = str(payload.get("last_agent_message") or "").strip()
                summary = message.splitlines()[0][:240] if message else None
                events = [_subagent_end(engine="codex", attrs=attrs, summary=summary)]
        elif payload.get("type") == "turn_aborted":
            reason = str(payload.get("reason") or "failed")
            status = reason if reason in {"interrupted", "cancelled"} else "failed"
            attrs = {"reason": reason}
            duration_ms = payload.get("duration_ms")
            if isinstance(duration_ms, (int, float)):
                attrs["duration_seconds"] = max(0, float(duration_ms) / 1000)
            events = [_subagent_end(engine="codex", status=status, attrs=attrs)]
    elif otype == "compacted":
        info = payload if isinstance(payload, dict) else {}
        data = {
            "trigger": info.get("trigger") or info.get("reason") or "automatic",
            "tokens_before": _int_or_zero(
                info.get("tokens_before") or info.get("input_tokens_before")
            ),
            "tokens_after": _int_or_zero(
                info.get("tokens_after") or info.get("input_tokens_after")
            ),
            "model_context_window": _int_or_zero(
                info.get("model_context_window") or info.get("context_window")
            ),
        }
        events = [HarnessEvent(
            HarnessEventKind.COMPACTION,
            text="Context compacted",
            data={key: value for key, value in data.items() if value not in (None, "")},
        )]
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
