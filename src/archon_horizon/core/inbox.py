"""Provider-agnostic inbox contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .clock import utc_now
from .labels import ARCHON_ACCEPT
from .types import Metadata


class InboxStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class InboxKind(StrEnum):
    HINT = "hint"
    ISSUE = "issue"
    QUESTION = "question"
    BLOCKER = "blocker"
    REVIEW = "review"
    PROPOSAL = "proposal"


@dataclass(frozen=True, slots=True)
class InboxScope:
    project: str | None = None
    file: str | None = None
    declaration: str | None = None


@dataclass(frozen=True, slots=True)
class InboxItem:
    id: str
    provider: str
    kind: InboxKind
    body: str
    labels: tuple[str, ...]
    status: InboxStatus = InboxStatus.OPEN
    scope: InboxScope = field(default_factory=InboxScope)
    source_ref: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboxDraft:
    kind: InboxKind
    body: str
    labels: tuple[str, ...] = (ARCHON_ACCEPT,)
    scope: InboxScope = field(default_factory=InboxScope)
    source_ref: str | None = None
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboxFilter:
    provider: str | None = None
    labels: tuple[str, ...] = ()
    status: InboxStatus | None = None
    kinds: tuple[InboxKind, ...] = ()
    project: str | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    provider: str
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    errors: tuple[str, ...] = ()


def matches_filter(item: InboxItem, filters: "InboxFilter | None") -> bool:
    """Pure predicate shared by every provider implementation."""
    if filters is None:
        return True
    if filters.provider is not None and item.provider != filters.provider:
        return False
    if filters.status is not None and item.status is not filters.status:
        return False
    if filters.kinds and item.kind not in filters.kinds:
        return False
    if filters.labels and not set(filters.labels).issubset(item.labels):
        return False
    if filters.project is not None and item.scope.project != filters.project:
        return False
    return True

