"""Shared inbox triage labels.

One axis of human triage, orthogonal to ``InboxStatus`` (open/closed) and to
``audience`` (who an item is for). Unlabeled means *new / untriaged*. The labels
are deliberately self-explanatory and double as GitHub labels.

- ``agent-ready``  — released: the AI agents may read and act on it.
- ``not-ready``    — read by a human but deliberately withheld from the agents.
- ``rejected``     — dismissed; the agents never see it.
"""

from __future__ import annotations

AGENT_READY = "agent-ready"
NOT_READY = "not-ready"
REJECTED = "rejected"

TRIAGE_LABELS = frozenset({AGENT_READY, NOT_READY, REJECTED})


def is_agent_ready(labels: set[str] | list[str] | tuple[str, ...]) -> bool:
    """Whether an inbox item should be visible to the AI agents."""
    label_set = set(labels)
    return AGENT_READY in label_set and NOT_READY not in label_set and REJECTED not in label_set
