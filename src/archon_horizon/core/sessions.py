"""Run/session contracts and fixed sync boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .clock import utc_now
from .types import Metadata


class SyncBoundary(StrEnum):
    BEFORE_GROUND = "before-ground"
    AFTER_GROUND = "after-ground"
    BEFORE_HORIZON = "before-horizon"
    AFTER_HORIZON = "after-horizon"
    ON_BUILD_FAILURE = "on-build-failure"
    BEFORE_PUBLISH = "before-publish"


FIXED_SYNC_BOUNDARIES: tuple[SyncBoundary, ...] = (
    SyncBoundary.BEFORE_GROUND,
    SyncBoundary.AFTER_GROUND,
    SyncBoundary.BEFORE_HORIZON,
    SyncBoundary.AFTER_HORIZON,
    SyncBoundary.ON_BUILD_FAILURE,
    SyncBoundary.BEFORE_PUBLISH,
)


@dataclass(frozen=True, slots=True)
class Focus:
    projects: tuple[str, ...] = ()
    task: str | None = None
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    focus: Focus = field(default_factory=Focus)
    rounds_requested: int = 1
    # Round index the run's first round is numbered from. 0 for a normal run;
    # a human driving one role at a time into an existing run (`horizon run
    # horizon --run 7 --round 3`) sets it so the session's round metadata and
    # commit trailer stay consistent with the automatic alternation.
    start_round: int = 0
    created_at: datetime = field(default_factory=utc_now)
    metadata: Metadata = field(default_factory=dict)
