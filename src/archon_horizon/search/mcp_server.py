"""A tiny, dependency-free MCP server exposing `lean_search` over stdio.

Claude Code (and any MCP client) launches this with the workspace as cwd; it
speaks newline-delimited JSON-RPC 2.0 — the MCP stdio transport — and exposes a
single ``lean_search`` tool backed by the same offline index as
``horizon search``. No third-party MCP SDK is required.

Run as: ``python -m archon_horizon.search.mcp_server`` (cwd = workspace root).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"

_TOOL = {
    "name": "lean_search",
    "description": (
        "Search local workspace/configured Lean declarations offline; for broad Mathlib search also try LeanSearch/Loogle/Moogle/LSP tools. "
        "Use mode='text' for natural language, 'name' for a declaration name, or 'type' for a "
        "signature pattern where ?a/?b/_ are wildcards. Returns name, kind, library, signature, "
        "and file:line for each hit."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Query text, name, or type pattern."},
            "mode": {"type": "string", "enum": ["text", "name", "type"], "default": "text"},
            "lib": {"type": "string", "description": "Restrict to one library/project name."},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
}


def _run_query(root: Path, args: dict) -> str:
    from archon_horizon.config.loader import load_config
    from archon_horizon.search.workspace import load_or_build_index

    cfg = load_config(root)
    index = load_or_build_index(root, cfg)
    query = str(args.get("query", ""))
    mode = args.get("mode", "text")
    lib = args.get("lib")
    limit = int(args.get("limit", 10))

    if mode == "name":
        hits = index.search_name(query, limit=limit, library=lib)
    elif mode == "type":
        hits = index.search_type(query, limit=limit, library=lib)
    else:
        hits = index.search_text(query, limit=limit, library=lib)

    if not hits:
        return f"No matches for {mode!r} query {query!r} across {len(index.declarations)} declarations."
    lines = []
    for h in hits:
        d = h.declaration
        lines.append(f"{d.name}  [{d.kind}, {d.library}]  {d.file}:{d.line}\n    {d.signature}")
        if d.doc:
            lines.append(f"    -- {d.doc[:200]}")
    return "\n".join(lines)


def _handle(message: dict, root: Path) -> dict | None:
    method = message.get("method")
    msg_id = message.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "archon-horizon-leansearch", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [_TOOL]}
    elif method == "tools/call":
        params = message.get("params", {})
        if params.get("name") != "lean_search":
            return _error(msg_id, -32602, f"unknown tool {params.get('name')!r}")
        try:
            text = _run_query(root, params.get("arguments", {}))
            result = {"content": [{"type": "text", "text": text}]}
        except Exception as exc:  # surface as a tool error, not a transport crash
            result = {"content": [{"type": "text", "text": f"lean_search failed: {exc}"}], "isError": True}
    elif method in ("notifications/initialized", "notifications/cancelled"):
        return None  # notification: no response
    elif method == "ping":
        result = {}
    else:
        return _error(msg_id, -32601, f"method not found: {method}")

    if msg_id is None:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main() -> int:
    root = Path.cwd()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            response = _handle(message, root)
        except Exception as exc:  # never let one bad request kill the loop
            response = _error(message.get("id"), -32603, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
