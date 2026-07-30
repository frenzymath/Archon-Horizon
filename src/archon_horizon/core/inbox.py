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
    # Soft-delete: kept for the record but hidden from the dashboard by default
    # (the user can opt to show archived items). Like delete, but non-destructive.
    ARCHIVED = "archived"


class InboxKind(StrEnum):
    # `hint` is what humans normally use; the rest are mostly for the AI.
    HINT = "hint"
    ISSUE = "issue"
    # A standing constraint on the Horizon agent (the soft "freeze"): e.g. "do not
    # change the signature of `Foo.bar`". Rendered in the prompt as protected;
    # respected, not hard-enforced, so semantic constraints are expressible.
    PROTECTION = "protection"
    # A direct or group thread whose replies should interrupt normal inbox
    # triage. Older workspaces encoded this only as metadata.conversation; keep
    # recognizing that shape through ``is_conversation`` below.
    CONVERSATION = "conversation"
    # An agent→human notice: something the agents did or noticed that the user
    # should know (a renamed project, an important change, a warning). Purely
    # informational — it never affects what the orchestrator runs.
    INFO = "info"
    # A durable note the agents keep (the memory channel lives in the inbox so a
    # human can prune it from the UI like any other item).
    MEMORY = "memory"


class InboxAttention(StrEnum):
    """Derived attention lane; it is not another user-maintained field."""

    REQUIRED = "required"
    CONVERSATION = "conversation"
    ADVISORY = "advisory"


InboxScope = ItemScope

# Metadata keys for the lightweight ownership/read-state overlay. Both live in
# ``metadata`` so they round-trip through the YAML store with no schema change.
OWNER_KEY = "owner_task"  # str task id; empty/absent means "owned by everyone"
READ_BY_KEY = "read_by"   # list[str] of reader ids (a task id, run id, or "human")
PARTICIPANTS_KEY = "participants"  # route targets, including the initiator
STARTED_BY_KEY = "started_by"      # canonical route target responsible for closure


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
    # anyone. Conventions: "horizon", "human", "project:<name>" ("ground" may
    # appear on items from older workspaces).
    audience: str = ""
    # Who wrote it (provenance for the UI to colour by): human / horizon
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


def item_owner(item: "InboxItem") -> str:
    """The task id this item is owned by, or ``""`` when owned by everyone.

    Ownership is a categorization aid: an owned item lives in one task's inbox
    (e.g. a private memory note); an unowned item is shared with all teams.
    """
    return str(item.metadata.get(OWNER_KEY) or "").strip()


def item_readers(item: "InboxItem") -> tuple[str, ...]:
    """Reader ids that have marked this item read (a task id, run id, or human)."""
    raw = item.metadata.get(READ_BY_KEY)
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(reader).strip() for reader in raw if str(reader).strip())


def is_read_by(item: "InboxItem", reader: str | None) -> bool:
    """Whether ``reader`` has read this item (False for an empty reader)."""
    reader = (reader or "").strip()
    return bool(reader) and reader in item_readers(item)


def is_conversation(item: "InboxItem") -> bool:
    """Recognize first-class and legacy metadata-backed conversations."""
    return item.kind is InboxKind.CONVERSATION or bool(item.metadata.get("conversation"))


def conversation_participants(item: "InboxItem") -> tuple[str, ...]:
    """Every route target taking part in a conversation, including its sender.

    New items persist this explicitly. The derivation keeps older conversations
    routable by recovering recipients, provenance, and human/role authorship.
    """
    if not is_conversation(item):
        return ()
    values: list[str] = list(audience_targets(item.audience))
    raw = item.metadata.get(PARTICIPANTS_KEY)
    if isinstance(raw, str):
        values.extend(audience_targets(raw))
    elif isinstance(raw, (list, tuple)):
        values.extend(str(value).strip() for value in raw if str(value).strip())
    started_by = str(item.metadata.get(STARTED_BY_KEY) or "").strip()
    if started_by:
        values.append(started_by)
    provenance = item.metadata.get("provenance")
    has_specific_origin = bool(started_by)
    if isinstance(provenance, dict):
        task = str(provenance.get("task") or "").strip()
        run = str(provenance.get("run") or "").strip()
        if task:
            values.append(f"task:{task}")
            has_specific_origin = True
        elif run:
            values.append(f"run:{run}")
            has_specific_origin = True
    author = str(item.author or "").strip().lower()
    if author == "human" or (author == "horizon" and not has_specific_origin):
        values.append(author)
    return tuple(dict.fromkeys(value for value in values if value))


def inbox_attention(item: "InboxItem") -> InboxAttention:
    """The fixed triage lane agents should use for this item."""
    if item.kind is InboxKind.PROTECTION:
        return InboxAttention.REQUIRED
    if is_conversation(item):
        return InboxAttention.CONVERSATION
    return InboxAttention.ADVISORY


def inbox_attention_rank(item: "InboxItem") -> int:
    """Sort protections first, then conversations, then advisory material."""
    return {
        InboxAttention.REQUIRED: 3,
        InboxAttention.CONVERSATION: 2,
        InboxAttention.ADVISORY: 1,
    }[inbox_attention(item)]


def audience_targets(audience: str | None) -> tuple[str, ...]:
    """Canonical recipients encoded in the backwards-compatible audience field.

    A single recipient keeps the historical spelling (``task:T-7``). Group
    conversations use a comma-separated list (``task:T-7, task:T-8, human``),
    avoiding a file-format migration while making each target independently
    routable.
    """
    if not audience:
        return ()
    return tuple(dict.fromkeys(
        target.strip() for target in str(audience).split(",") if target.strip()
    ))


def reaches_horizon(
    item: "InboxItem",
    project: str | None,
    *,
    task: str | None = None,
    run: str | None = None,
    inbox_refs: tuple[str, ...] = (),
) -> bool:
    """Whether an item should be injected for the Horizon agent on ``project``.

    Horizon sees general items, items addressed to it, or items for its project.
    An item *owned* by a task reaches only that task (``task`` known); an item
    addressed to task/run recipients is a direct or group message. A task's
    explicit ``inbox_refs`` are a deliberate read grant that OVERRIDES the
    project-scope and ownership vetoes: a task that linked an item to itself must
    be able to read it even when the item is scoped to (or owned by) another
    project/task — otherwise the CLI accepts a comment on the thread but refuses
    to show it (I-0489). The originating task/run remains a participant, so it
    can follow replies after sending a DM.
    """
    if item.id in inbox_refs:
        return True
    scoped_projects = item.scope.targets("projects")
    if scoped_projects and project not in scoped_projects:
        return False
    owner = item_owner(item)
    if owner and task is not None and owner != task:
        return False
    targets = audience_targets(item.audience)
    participants = conversation_participants(item)
    if not targets or "horizon" in targets:
        return True
    routes = participants or targets
    if "horizon" in routes:
        return True
    if project is not None and f"project:{project}" in routes:
        return True
    if task is not None and f"task:{task}" in routes:
        return True
    if run is not None and f"run:{run}" in routes:
        return True
    provenance = item.metadata.get("provenance")
    if isinstance(provenance, dict):
        if task is not None and str(provenance.get("task") or "") == task:
            return True
        if run is not None and str(provenance.get("run") or "") == run:
            return True
    return False


@dataclass(frozen=True, slots=True)
class InboxFilter:
    provider: str | None = None
    labels: tuple[str, ...] = ()
    status: InboxStatus | None = None
    kinds: tuple[InboxKind, ...] = ()
    project: str | None = None
    audience: str | None = None
    query: str = ""
    # A task's inbox: items owned by this task PLUS shared (unowned) items.
    owner_task: str | None = None
    # Keep only items this reader has not marked read (a task id, run id, human).
    unread_for: str | None = None


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
    if filters.audience is not None:
        if filters.audience:
            if filters.audience not in audience_targets(item.audience):
                return False
        elif item.audience:
            return False
    if filters.owner_task is not None:
        owner = item_owner(item)
        if owner and owner != filters.owner_task:
            return False
    if filters.unread_for is not None and is_read_by(item, filters.unread_for):
        return False
    query = filters.query.strip().lower()
    if query:
        scope_text = " ".join(
            value
            for key in ("projects", "files", "declarations")
            for value in item.scope.targets(key)
        )
        comments_text = " ".join(
            str(comment.get("body") or "")
            for comment in item.metadata.get("comments", [])
            if isinstance(comment, dict)
        )
        haystack = " ".join(
            str(value)
            for value in (
                item.id,
                item.provider,
                item.kind.value,
                item.status.value,
                item.body,
                " ".join(item.labels),
                item.audience,
                item.author,
                item.source_ref or "",
                scope_text,
                comments_text,
            )
        ).lower()
        if query not in haystack:
            return False
    return True
