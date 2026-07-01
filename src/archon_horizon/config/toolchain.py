"""Lean toolchain detection helpers (pure; no I/O of their own).

``setup`` provisions the Lean toolchain once so runs never spend a session
installing it. The detection logic is factored out here so it is testable
without touching the real system.
"""

from __future__ import annotations

from collections.abc import Callable

LEAN_TOOLS = ("elan", "lean", "lake")
ELAN_INSTALL_HINT = "curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y"


def lean_toolchain_report(which: Callable[[str], str | None]) -> dict[str, bool]:
    """Map each Lean tool to whether ``which`` can find it on PATH."""
    return {tool: which(tool) is not None for tool in LEAN_TOOLS}


def missing_lean_tools(report: dict[str, bool]) -> tuple[str, ...]:
    return tuple(tool for tool, present in report.items() if not present)
