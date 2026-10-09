"""Bounded public provider output for the operator activity stream."""

from __future__ import annotations

import json
import hashlib
import re
from uuid import UUID, uuid5


_SECRET = re.compile(r'''(?ix)(\b(?:authorization|proxy-authorization)\s*["']?\s*[:=]\s*["']?\s*(?:(?:bearer|basic|token)\s+)?)[^\s"'\\,;]+|(\b(?:token|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|HORIZON_EXECUTION_TOKEN)\s*["']?\s*[:=]\s*["']?)[^\s"'\\,;]+''')
_URL_AUTH = re.compile(r"(https?://)[^/\s:@]+:[^/\s@]+@", re.I)
_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|hz_[A-Za-z0-9_-]{24,})\b")
_BEARER = re.compile(r"\b(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I)
_ARGUMENT_SECRET = re.compile(r'''(?i)(--(?:api-key|token|password|secret)(?:=|\s+)["']?)[^\s"']+''')
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S)
_CATEGORIES = {"message", "tool", "lifecycle", "failure"}
_SKILL_PATH = re.compile(r"((?:operations|lean|review|custom)/[A-Za-z0-9_.-]+/SKILL\.md)")


def safe_text(value, limit=12000):
    if not isinstance(value, str):
        return ""
    # Redact before clipping so a secret crossing the boundary is not exposed.
    value = value[:65536]
    value = _PRIVATE_KEY.sub("[private key redacted]", value)
    value = _SECRET.sub(lambda match: (match[1] or match[2]) + "[redacted]", value)
    value = _BEARER.sub(r"\1[redacted]", value)
    value = _ARGUMENT_SECRET.sub(r"\1[redacted]", value)
    value = _TOKEN.sub("[redacted]", _URL_AUTH.sub(r"\1[redacted]@", value))
    value = "".join(c for c in value if c in "\n\t" or ord(c) >= 32)
    return value[:limit] + ("\n[truncated]" if len(value) > limit else "")


def normalize_skills(value):
    """Return bounded, stable skill names suitable for activity telemetry."""
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({item.strip()[:128] for item in value
                   if isinstance(item, str) and item.strip()})[:32]


def skills_referenced(display):
    """Infer only explicit pinned skill-file references from a tool command.

    Provider output is untrusted and may quote arbitrary paths. Restrict the
    scan to the command prefix, before its exit marker, and record the stable
    skill name rather than exposing the command or filesystem path.
    """
    if not isinstance(display, dict) or display.get("category") != "tool":
        return []
    detail = display.get("detail")
    if not isinstance(detail, str):
        return []
    command = detail.split("\nExit code:", 1)[0]
    names = {path.rsplit("/", 2)[-2] for path in _SKILL_PATH.findall(command)}
    return normalize_skills(names)


def _json(value):
    return safe_text(json.dumps(value, ensure_ascii=True, default=str))


def display_event(adapter, raw):
    """Select visible messages and tool results, never reasoning or user prompts."""
    if not isinstance(raw, dict):
        return None
    existing = raw.get("horizon_activity")
    if isinstance(existing, dict) and existing.get("category") in _CATEGORIES:
        return {"key": safe_text(existing.get("key"), 256), "category": existing["category"],
                "title": safe_text(existing.get("title"), 240), "detail": safe_text(existing.get("detail"))}
    kind = raw.get("type")
    key, category, title, detail = "", "", "", ""
    if adapter == "codex" and kind in ("item.started", "item.completed"):
        item = raw.get("item")
        if not isinstance(item, dict):
            return None
        item_kind = item.get("type")
        key = str(item.get("id", "")) + ":" + kind
        if item_kind == "agent_message" and kind == "item.completed":
            category, detail = "message", item.get("text", "")
            title = safe_text(detail, 240).split("\n")[0]
        elif item_kind == "command_execution":
            category = "tool"
            command = safe_text(item.get("command"), 180)
            status = "Started" if kind == "item.started" else "Finished"
            title = f"{status}: {command}"
            detail = f"Command: {safe_text(item.get('command'))}\n"
            if item.get("exit_code") is not None:
                detail += f"Exit code: {item['exit_code']}\n"
            detail += safe_text(item.get("aggregated_output"))
        elif item_kind in ("mcp_tool_call", "dynamic_tool_call", "web_search", "file_change"):
            category = "tool"
            name = item.get("tool", item.get("type", "Tool"))
            title = f"{name}: {'started' if kind == 'item.started' else 'completed'}"
            detail = _json({k: item[k] for k in ("server", "tool", "arguments", "result", "error", "query", "changes") if k in item})
    elif adapter == "claude" and kind in ("assistant", "user"):
        message = raw.get("message")
        if not isinstance(message, dict):
            return None
        content = message.get("content")
        blocks = []
        for block in content[:32] if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if kind == "assistant" and block.get("type") == "text":
                blocks.append(safe_text(block.get("text")))
            elif block.get("type") == "tool_use":
                blocks.append(f"Tool: {safe_text(block.get('name'), 120)}\n{_json(block.get('input'))}")
                category = "tool"
            elif block.get("type") == "tool_result":
                blocks.append(f"Tool result: {safe_text(block.get('tool_use_id'), 120)}\n{_json(block.get('content'))}")
                category = "tool"
        if blocks:
            category = category or "message"
            title = "Agent message" if category == "message" else "Agent tool activity"
            detail = "\n\n".join(blocks)
            key = str(message.get("id") or raw.get("uuid") or "")
            if key:
                # Claude may emit text and tool blocks separately under one
                # message ID; repeated usage snapshots must still deduplicate.
                key += ":" + hashlib.sha256(detail.encode()).hexdigest()[:16]
    elif kind in ("error", "turn.failed"):
        category, title, detail = "failure", "Provider error", _json(raw.get("error", raw.get("message")))
        key = str(raw.get("id", ""))
    if not category or not detail:
        return None
    return {"key": safe_text(key, 256), "category": category, "title": safe_text(title, 240), "detail": safe_text(detail)}


def activity_identity(request_id, display, operation_id):
    key = display.get("key")
    return uuid5(UUID(str(request_id)), "activity:" + key) if key and key not in (":item.completed", ":item.started") else UUID(str(operation_id))


def select_provider_event(adapter, raw):
    from ..providers.provider_events import select_native_event

    native = select_native_event(adapter, raw)
    display = display_event(adapter, raw)
    if display:
        return {**(native or {"type": "horizon.activity"}), "horizon_activity": display}
    return native
