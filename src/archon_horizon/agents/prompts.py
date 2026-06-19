"""Prompt composition — pure, light, and engine-agnostic.

Per the roadmap both agents get a deliberately small default context: the
roadmap (sliced for Horizon), accepted inbox items, and a tiny memory file.
Full logs stay as artifacts and are pulled only on demand. These functions
are plain data->string transforms so they are trivial to test and to swap.
"""

from __future__ import annotations

from archon_horizon.core.inbox import InboxItem
from archon_horizon.core.roadmap import Roadmap

from .base import HorizonContext, InformalContext


def _roadmap_lines(roadmap: Roadmap) -> str:
    if not roadmap.items:
        return "(roadmap is empty)"
    return "\n".join(
        f"- {item.id} [{item.status}] {item.title}"
        + (f" — {item.summary}" if item.summary else "")
        for item in roadmap.items
    )


def _inbox_lines(items: tuple[InboxItem, ...]) -> str:
    if not items:
        return "(no accepted inbox items)"
    return "\n".join(f"- {item.id} ({item.kind}): {item.body}" for item in items)


def compose_informal_prompt(context: InformalContext) -> str:
    return (
        "You are the informal agent. Maintain human-facing state and decide "
        "what Horizon should do next. Keep output compact.\n\n"
        f"# Roadmap\n{_roadmap_lines(context.roadmap)}\n\n"
        f"# Accepted inbox\n{_inbox_lines(context.accepted_inbox)}\n\n"
        f"# Memory\n{context.memory or '(empty)'}\n\n"
        "Emit any roadmap/memory/task updates as a trailing ```json block "
        "(see the update schema). Prose above it is your report."
    )


def compose_horizon_prompt(context: HorizonContext) -> str:
    task = context.task
    return (
        "You are the Horizon agent. Complete exactly one task. Write Lean, run "
        "build tools, repair failures. Keep your final report short.\n\n"
        f"# Task {task.id} (project: {task.project})\n{task.objective}\n\n"
        f"# Roadmap (sliced)\n{_roadmap_lines(context.roadmap)}\n\n"
        f"# Memory\n{context.memory or '(empty)'}"
    )
