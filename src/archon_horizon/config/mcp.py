"""Generate the harness MCP config (``.mcp.json``) for Lean tooling.

Claude Code reads ``.mcp.json`` from the working directory and surfaces the
declared MCP servers to the agent. We register the Lean language-server MCP so
the Horizon agent gets live diagnostics / hover / go-to-definition instead of
shelling ``lake build`` repeatedly. The server binary itself is external
(installed by ``setup``); here we only declare how to launch it. Merging is
non-destructive so hand-added servers survive a re-init.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .schema import HarnessConfig

# Launched via uvx so no global install is required; setup verifies availability.
LEAN_LSP_SERVER: dict[str, Any] = {"command": "uvx", "args": ["lean-lsp-mcp"]}

# Our own offline declaration search (mathlib + configured external_libraries).
# Runs from the bundled package via the current interpreter — no extra install,
# no GPU/API key. cwd is the workspace root (where Claude Code launches it).
LEANSEARCH_SERVER: dict[str, Any] = {
    "command": sys.executable,
    "args": ["-m", "archon_horizon.search.mcp_server"],
}


def default_mcp_servers() -> dict[str, dict[str, Any]]:
    return {"lean-lsp": dict(LEAN_LSP_SERVER), "leansearch": dict(LEANSEARCH_SERVER)}


def merge_mcp_config(existing: dict[str, Any], servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Add ``servers`` without clobbering ones already present."""
    out = dict(existing)
    merged = dict(out.get("mcpServers", {}))
    for name, cfg in servers.items():
        merged.setdefault(name, cfg)
    out["mcpServers"] = merged
    return out


def write_mcp_config(path: Path, servers: dict[str, dict[str, Any]] | None = None) -> list[str]:
    """Merge the given (or default) servers into ``path``; return server names."""
    servers = default_mcp_servers() if servers is None else servers
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text("utf-8")) or {}
        except json.JSONDecodeError:
            existing = {}
    merged = merge_mcp_config(existing, servers)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", "utf-8")
    return sorted(merged["mcpServers"])


# ── per-engine MCP registration (so the same servers reach every harness) ──
#
# There is no universal MCP-registration file across engines, so we keep ONE
# canonical server list (``default_mcp_servers``) and write it into each engine's
# own location. Both are workspace-local — only auth lives in the config home:
#   - Claude Code → project ``.mcp.json`` (written at the workspace root)
#   - Codex       → ``[mcp_servers.*]`` in the workspace ``.codex/config.toml``


def _toml_str(value: str) -> str:
    """Minimal TOML basic-string escaping for our known-simple values."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_mcp_toml_block(name: str, spec: dict[str, Any]) -> str:
    """The ``[mcp_servers.<name>]`` TOML block for one stdio server. Pure (testable)."""
    args = ", ".join(_toml_str(str(a)) for a in spec.get("args", []))
    return (
        f"[mcp_servers.{name}]\n"
        f"command = {_toml_str(str(spec['command']))}\n"
        f"args = [{args}]\n"
    )


def _existing_codex_servers(path: Path) -> set[str]:
    """Names already declared under ``[mcp_servers]`` in a Codex ``config.toml``."""
    if not path.exists():
        return set()
    try:
        import tomllib

        data = tomllib.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return set()
    servers = data.get("mcp_servers")
    return set(servers) if isinstance(servers, dict) else set()


def write_codex_mcp_config(
    codex_dir: Path, servers: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    """Append our servers to the workspace ``<codex_dir>/config.toml`` (non-destructive).

    Only ``[mcp_servers.<name>]`` blocks not already present are appended, so
    hand-added servers and every other config table are preserved verbatim.
    Returns the names newly added.
    """
    servers = default_mcp_servers() if servers is None else servers
    path = codex_dir / "config.toml"
    present = _existing_codex_servers(path)
    blocks: list[str] = []
    added: list[str] = []
    for name, spec in servers.items():
        if name in present:
            continue
        blocks.append(codex_mcp_toml_block(name, spec))
        added.append(name)
    if blocks:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text("utf-8") if path.exists() else ""
        sep = existing.rstrip() + "\n\n" if existing.strip() else ""
        path.write_text(sep + "\n".join(blocks), "utf-8")
    return added


def install_mcp_for_harnesses(
    harness_configs: dict[str, HarnessConfig],
    workspace_root: Path,
    servers: dict[str, dict[str, Any]] | None = None,
) -> dict[str, list[str]]:
    """Register the canonical servers with every non-Claude harness in the config.

    Claude reads the project ``.mcp.json`` (written separately at the workspace
    root), so it isn't handled here. Codex gets the servers in the workspace
    ``.codex/config.toml``. Returns a map of a human label → server names added.
    """
    servers = default_mcp_servers() if servers is None else servers
    out: dict[str, list[str]] = {}
    if any(cfg.kind == "codex" for cfg in harness_configs.values()):
        out["codex"] = write_codex_mcp_config(workspace_root / ".codex", servers)
    return out
