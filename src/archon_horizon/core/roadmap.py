"""Structured roadmap contracts."""

from __future__ import annotations

from collections.abc import Sequence
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


# ── Hierarchy ───────────────────────────────────────────────────────────────
#
# A roadmap item can nest under another so the roadmap reads as an outline
# (goal → sub-goal → step) instead of a flat list that feels the same as the
# task queue. Nesting is stored in ``metadata`` — ``parent`` (an item id) and/or
# ``depth`` (an indentation level) — so it needs no schema change and round-trips
# through serde untouched. ``parent`` is the real tree link; ``depth`` is the
# lightweight fallback (markdown-heading style) when no parent is set.


def item_parent(item: RoadmapItem) -> str:
    """The explicit parent item id, or ``""`` for a top-level item."""
    raw = item.metadata.get("parent")
    return str(raw).strip() if raw else ""


def apply_hierarchy(
    metadata: Metadata, *, depth: int | None = None, parent: str | None = None
) -> dict:
    """Return a copy of ``metadata`` with the hierarchy keys updated. ``depth``/
    ``parent`` are only touched when not ``None``; ``parent=""`` clears it (un-nest).
    Shared by the CLI and the dashboard so both write nesting the same way."""
    meta = dict(metadata)
    if depth is not None:
        meta["depth"] = max(0, int(depth))
    if parent is not None:
        stripped = str(parent).strip()
        if stripped:
            meta["parent"] = stripped
        else:
            meta.pop("parent", None)
    return meta


def item_depth(item: RoadmapItem) -> int:
    """The configured indentation depth (0 = top level). Used as the item's depth
    only when no ``parent`` link places it in the tree."""
    raw = item.metadata.get("depth")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def ordered_tree(items: Sequence[RoadmapItem]) -> list[tuple[RoadmapItem, int]]:
    """Items in pre-order tree traversal, each paired with its *effective* depth.

    An item nests under its ``parent`` when that parent is present (effective depth
    = parent's depth + 1); an item with no resolvable parent is a root whose depth
    is its configured ``depth`` metadata. Siblings and roots are ordered by id, so
    dotted ids like ``A``, ``A.1``, ``A.1.1``, ``A.2`` nest naturally with no parent
    links at all. Every item is emitted exactly once; a parent cycle or dangling
    parent degrades gracefully to a root."""
    by_id = {it.id: it for it in items}
    children: dict[str, list[RoadmapItem]] = {}
    roots: list[RoadmapItem] = []
    for it in items:
        parent = item_parent(it)
        if parent and parent in by_id and parent != it.id:
            children.setdefault(parent, []).append(it)
        else:
            roots.append(it)
    for kids in children.values():
        kids.sort(key=lambda i: i.id)
    roots.sort(key=lambda i: i.id)

    out: list[tuple[RoadmapItem, int]] = []
    seen: set[str] = set()

    def walk(node: RoadmapItem, depth: int) -> None:
        if node.id in seen:  # parent cycle guard
            return
        seen.add(node.id)
        out.append((node, depth))
        for child in children.get(node.id, ()):
            walk(child, depth + 1)

    for root in roots:
        walk(root, item_depth(root))
    for it in items:  # anything trapped in a cycle: emit as a root fallback
        if it.id not in seen:
            seen.add(it.id)
            out.append((it, item_depth(it)))
    return out


def subtree(items: Sequence[RoadmapItem], root_id: str) -> list[tuple[RoadmapItem, int]]:
    """The ``root_id`` item and its descendants, in tree order, depths rebased so
    the root is 0 — for ``roadmap list --focus <id>``. Empty if the id is unknown."""
    full = ordered_tree(items)
    start = next((i for i, (it, _) in enumerate(full) if it.id == root_id), None)
    if start is None:
        return []
    base_depth = full[start][1]
    picked = [(full[start][0], 0)]
    for it, depth in full[start + 1:]:
        if depth <= base_depth:
            break  # left the subtree
        picked.append((it, depth - base_depth))
    return picked
