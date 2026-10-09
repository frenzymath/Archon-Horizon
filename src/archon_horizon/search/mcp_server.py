"""A tiny MCP server exposing `lean_search` over stdio.

Claude Code (and any MCP client) launches this with the workspace as cwd; it
speaks newline-delimited JSON-RPC 2.0 — the MCP stdio transport — and exposes a
single ``lean_search`` tool backed by Horizon's offline index.
Ranking uses bm25s; no third-party MCP SDK is required.

Run as: ``python -m archon_horizon.search.mcp_server`` (cwd = workspace root).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
# Keep one tool response bounded even if a client bypasses JSON Schema checks.
MAX_RESULTS = 100

_TOOL = {
    "name": "lean_search",
    "description": (
        "Search Lean declarations already on this worker: the checkout, nested Lake projects, and fetched .lake/packages. "
        "This is not LeanSearch.net. For informal Mathlib use lean-lsp lean_leansearch; for type-shaped Mathlib use lean_loogle. "
        "Use mode='text' for natural language over names/signatures/docstrings/module headers, 'name' for a declaration name, "
        "'type' for a signature pattern where ?a/?b/_ are wildcards, or 'header' for module comments. "
        "Returns name, kind, library, signature, and file:line. Use mode='libraries' to list indexed library names. "
        "This index complements ls/rg in the same tree and extra-library clones; an empty hit list is not proof the source is empty. "
        "Prefer rg/grep for a known identifier in this worktree; this index does not see unfetched packages."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Query text, name, type pattern, or module-header text."},
            "mode": {"type": "string", "enum": ["text", "name", "type", "header", "libraries"], "default": "text"},
            "lib": {"type": "string", "description": "Restrict to one library/project name."},
            "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": MAX_RESULTS},
        },
    },
}


def _run_query(root: Path, args: dict) -> str:
    from archon_horizon.search.workspace import load_or_build_index

    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")
    query = args.get("query", "")
    mode = args.get("mode", "text")
    limit = args.get("limit", 10)
    if not isinstance(query, str) or mode not in ("text", "name", "type", "header", "libraries"):
        raise ValueError("Use a string query and a supported search mode")
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"limit must be an integer between 1 and {MAX_RESULTS}")
    index = load_or_build_index(root)
    if mode == "libraries":
        return json.dumps(index.library_counts, indent=2)
    lib = args.get("lib")

    if mode == "name":
        hits = index.search_name(query, limit=limit, library=lib)
    elif mode == "type":
        hits = index.search_type(query, limit=limit, library=lib)
    elif mode == "header":
        hits = index.search_header(query, limit=limit, library=lib)
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
        elif d.header:
            lines.append(f"    /-! {d.header[:200]}")
    return "\n".join(lines)


def _handle(message: dict, root: Path) -> dict | None:
    # JSON scalars and arrays are valid JSON but invalid MCP requests. Validate
    # before exception reporting too, so one malformed line cannot kill stdio.
    if not isinstance(message, dict) or not isinstance(message.get("method"), str):
        return _error(None, -32600, "Invalid request")
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(msg_id, -32602, "params must be an object") if "id" in message else None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "archon-horizon-leansearch", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [_TOOL]}
    elif method == "tools/call":
        if params.get("name") != "lean_search":
            return _error(msg_id, -32602, f"unknown tool {params.get('name')!r}") if "id" in message else None
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
        return _error(msg_id, -32601, f"method not found: {method}") if "id" in message else None

    if "id" not in message:
        return None
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def main() -> int:
    root = Path(os.environ.get("HORIZON_WORKSPACE") or Path.cwd())
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
            continue
        try:
            response = _handle(message, root)
        except Exception as exc:  # never let one bad request kill the loop
            response = _error(message.get("id") if isinstance(message, dict) else None, -32603, str(exc))
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
