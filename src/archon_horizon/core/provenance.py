"""Structured provenance derived from a running Horizon session."""

from __future__ import annotations

import os


def agent_provenance() -> dict[str, str] | None:
    """Return the current agent run/session identity, if this is an agent run."""
    fields = {
        "run": os.environ.get("ARCHON_HORIZON_RUN", "").strip(),
        "session": os.environ.get("ARCHON_HORIZON_SESSION", "").strip(),
        "role": os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower(),
        "subagent": os.environ.get("ARCHON_HORIZON_SUBAGENT", "").strip(),
        "round": os.environ.get("ARCHON_HORIZON_ROUND", "").strip(),
        "rounds": os.environ.get("ARCHON_HORIZON_ROUNDS", "").strip(),
        "task": os.environ.get("ARCHON_HORIZON_TASK", "").strip(),
        "task_title": os.environ.get("ARCHON_HORIZON_TASK_TITLE", "").strip(),
        "projects": os.environ.get("ARCHON_HORIZON_PROJECTS", "").strip(),
    }
    provenance = {key: value for key, value in fields.items() if value}
    return provenance or None
