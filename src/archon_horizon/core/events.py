"""Append-only event contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import utc_now
from .types import Metadata


@dataclass(frozen=True, slots=True)
class Event:
    """One durable state transition.

    Events are intentionally generic. Subsystems should use stable dotted event
    names such as ``inbox.synced`` or ``horizon.task.completed``.
    """

    type: str
    id: str
    created_at: datetime = field(default_factory=utc_now)
    actor: str = "system"
    data: Metadata = field(default_factory=dict)

