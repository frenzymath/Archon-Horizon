"""Structured roadmap contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .clock import utc_now
from .scope import ItemScope
from .types import Metadata


class RoadmapStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    BLOCKED = "blocked"
    DONE = "done"
    REJECTED = "rejected"


class RoadmapKind(StrEnum):
    PROOF = "proof"
    BLUEPRINT = "blueprint"
    REFACTOR = "refactor"
    WORKSPACE = "workspace"
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class RoadmapItem:
    id: str
    title: str
    projects: tuple[str, ...]
    summary: str = ""
    status: RoadmapStatus = RoadmapStatus.PENDING
    kind: RoadmapKind = RoadmapKind.PROOF
    priority: str = "normal"
    depends_on: tuple[str, ...] = ()
    inbox_refs: tuple[str, ...] = ()
    task_refs: tuple[str, ...] = ()
    scope: ItemScope = field(default_factory=ItemScope)
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Roadmap:
    version: int = 1
    updated_at: datetime = field(default_factory=utc_now)
    items: tuple[RoadmapItem, ...] = ()

    def slice_for_projects(self, projects: set[str]) -> "Roadmap":
        """Return a focused roadmap: items touching the projects plus their
        direct dependencies (which may live in other projects), as the
        roadmap's focused-run slice requires.
        """
        in_focus = [item for item in self.items if projects.intersection(item.projects)]
        wanted = {item.id for item in in_focus}
        for item in in_focus:
            wanted.update(item.depends_on)
        return Roadmap(
            version=self.version,
            updated_at=self.updated_at,
            items=tuple(item for item in self.items if item.id in wanted),
        )
