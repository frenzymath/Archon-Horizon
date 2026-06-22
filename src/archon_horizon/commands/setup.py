"""Typer-decorated ``setup`` entry point."""

from __future__ import annotations

import shutil
import subprocess
import sys

import typer

from archon_horizon.log import log


def setup(
    ctx: typer.Context,
) -> None:
    """Install external dependencies (Claude Code, etc)."""
    log.header("Checking system tools...")
    
    # Check Python
    v = sys.version_info
    if v >= (3, 11):
        log.success(f"Python {v.major}.{v.minor} is installed.")
    else:
        log.error(f"Python {v.major}.{v.minor} is too old. Need 3.11+")

    # Check and install Claude Code
    if shutil.which("claude"):
        log.success("Claude Code is already installed.")
    else:
        log.warn("Claude Code is not installed. Attempting to install via npm...")
        if shutil.which("npm"):
            try:
                subprocess.run(["npm", "install", "-g", "@anthropic-ai/claude-code"], check=True)
                log.success("Successfully installed Claude Code.")
            except subprocess.CalledProcessError:
                log.error("Failed to install Claude Code. Please run `npm install -g @anthropic-ai/claude-code` manually.")
        else:
            log.error("npm is not installed. Cannot automatically install Claude Code.")

    # Check Antigravity (agy)
    if shutil.which("agy"):
        log.success("Antigravity (agy) is already installed.")
    else:
        log.warn("Antigravity (agy) is not installed. You can install it if you plan to use it as a harness.")

    log.rule()
    log.success("Setup complete. You are ready to run `horizon init` in your projects!")
