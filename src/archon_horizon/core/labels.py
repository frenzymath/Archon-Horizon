"""Shared inbox labels.

Labels intentionally mirror the GitHub-facing labels from the roadmap while
remaining provider-agnostic.
"""

from __future__ import annotations

ARCHON_ACCEPT = "archon:accept"
ARCHON_PENDING = "archon:pending"
ARCHON_REJECTED = "archon:rejected"

ARCHON_LABELS = frozenset({ARCHON_ACCEPT, ARCHON_PENDING, ARCHON_REJECTED})


def is_accepted(labels: set[str] | list[str] | tuple[str, ...]) -> bool:
    """Return whether an inbox item should be visible to agents."""
    label_set = set(labels)
    return ARCHON_ACCEPT in label_set and ARCHON_PENDING not in label_set and ARCHON_REJECTED not in label_set

