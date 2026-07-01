"""Typer-decorated ``setup`` entry point."""

from __future__ import annotations

import shutil
import subprocess
import sys

import typer

from archon_horizon.config.harnesses import CLAUDE_P_INSTALL_HINT
from archon_horizon.config.toolchain import ELAN_INSTALL_HINT, lean_toolchain_report, missing_lean_tools
from archon_horizon.log import log

from .shared import emit_json, github_cli_status


def setup(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Install external dependencies (Claude Code, etc)."""
    checks: list[dict] = []

    def record(tool: str, status: str, detail: str) -> None:
        checks.append({"tool": tool, "status": status, "detail": detail})
        {"ok": log.success, "warn": log.warn, "error": log.error}.get(status, log.info)(detail)

    log.header("Checking system tools...")

    # Check Python
    v = sys.version_info
    if v >= (3, 11):
        record("python", "ok", f"Python {v.major}.{v.minor} is installed.")
    else:
        record("python", "error", f"Python {v.major}.{v.minor} is too old. Need 3.11+")

    # Check and install Claude Code
    if shutil.which("claude"):
        record("claude", "ok", "Claude Code is already installed.")
    else:
        log.warn("Claude Code is not installed. Attempting to install via npm...")
        if shutil.which("npm"):
            try:
                subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"], check=True)
                record("claude", "ok", "Successfully installed Claude Code.")
            except subprocess.CalledProcessError:
                record("claude", "error", "Failed to install Claude Code. Please run `npm install -g @anthropic-ai/claude-code` manually.")
        else:
            record("claude", "error", "npm is not installed. Cannot automatically install Claude Code.")

    # Optional Claude Code TUI backend
    if shutil.which("claude-p"):
        record("claude-p", "ok", "claude-p is installed for the optional Claude Code TUI backend.")
    else:
        record("claude-p", "warn", f"{CLAUDE_P_INSTALL_HINT}\nOnly needed if you use options.backend: claude-p.")

    if shutil.which("codex"):
        record("codex", "ok", "Codex is already installed.")
    else:
        record("codex", "warn", "Codex is not installed. Install it if you plan to use a codex harness.")

    # GitHub CLI — needed for the GitHub inbox sync and repo status.
    gh_status, gh_detail = github_cli_status()
    record("gh", gh_status, gh_detail)

    # Lean toolchain — provisioned once here so runs never install it mid-session.
    report = lean_toolchain_report(shutil.which)
    missing = missing_lean_tools(report)
    if not missing:
        record("lean", "ok", "Lean toolchain (elan, lean, lake) is installed.")
    else:
        record(
            "lean",
            "warn",
            f"Lean toolchain missing: {', '.join(missing)}. Install elan (provides lean+lake):\n  {ELAN_INSTALL_HINT}",
        )

    # Lean LSP MCP server (exposed to the Horizon agent via .mcp.json on `init`).
    if shutil.which("uvx"):
        record("uvx", "ok", "uvx is available to launch the lean-lsp MCP server (uvx lean-lsp-mcp).")
    else:
        record("uvx", "warn", "uvx not found; install uv so the lean-lsp MCP server can launch (pip install uv).")

    if as_json:
        emit_json({"checks": checks, "ok": all(c["status"] != "error" for c in checks)})
        return

    log.header("Setup Status")
    log.success("Setup complete. You are ready to run `horizon init` in your projects!")
