"""Provider-agnostic inbox contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .clock import utc_now
from .labels import AGENT_READY
from .scope import ItemScope
from .types import Metadata


class InboxStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class InboxKind(StrEnum):
    # `hint` is what humans normally use; the rest are mostly for the AI.
    HINT = "hint"
    ISSUE = "issue"
    # A standing constraint on the Horizon agent (the soft "freeze"): e.g. "do not
    # change the signature of `Foo.bar`". Rendered in the prompt as protected;
    # respected, not hard-enforced, so semantic constraints are expressible.
    PROTECTION = "protection"
    # An agent→human notice: something the agents did or noticed that the user
    # should know (a renamed project, an important change, a warning). Purely
    # informational — it never affects what the orchestrator runs.
    INFO = "info"
    # A durable note the agents keep (the memory channel lives in the inbox so a
    # human can prune it from the UI like any other item).
    MEMORY = "memory"


InboxScope = ItemScope


@dataclass(frozen=True, slots=True)
class InboxItem:
    id: str
    provider: str
    kind: InboxKind
    body: str
    labels: tuple[str, ...]
    status: InboxStatus = InboxStatus.OPEN
    scope: InboxScope = field(default_factory=InboxScope)
    # Who the item is FOR (vs ``scope``, which is what it is ABOUT). Empty means
    # anyone. Conventions: "horizon", "ground", "human", "project:<name>".
    audience: str = ""
    # Who wrote it (provenance for the UI to colour by): human / horizon / ground
    # / github / <name>. Distinct from ``audience``.
    author: str = ""
    source_ref: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InboxDraft:
    kind: InboxKind
    body: str
    labels: tuple[str, ...] = (AGENT_READY,)
    scope: InboxScope = field(default_factory=InboxScope)
    audience: str = ""
    author: str = ""
    source_ref: str | None = None
    metadata: Metadata = field(default_factory=dict)


def reaches_horizon(item: "InboxItem", project: str | None) -> bool:
    """Whether an item should be injected for the Horizon agent on ``project``.

    The Ground agent triages everything, so it has no such filter; Horizon
    only sees general items, items addressed to it, or items for its project.
    """
    scoped_projects = item.scope.targets("projects")
    if scoped_projects and project not in scoped_projects:
        return False
    audience = item.audience
    if not audience or audience == "horizon":
        return True
    return project is not None and audience == f"project:{project}"


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
    if filters.project is not None and filters.project not in item.scope.targets("projects"):
        return False
    return True
