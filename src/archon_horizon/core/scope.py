"""Shared scoped-target metadata for inbox, roadmap, and tasks.

The stored shape is intentionally compact: each scope list may contain bare
strings for the common writable case, or objects when a target needs attributes
such as read-only access or frozen layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .types import Metadata


class ScopeAccess(StrEnum):
    READ = "read"
    WRITE = "write"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True, slots=True)
class ScopeEntry:
    target: str
    access: ScopeAccess = ScopeAccess.WRITE
    role: str = ""
    layers: Metadata = field(default_factory=dict)
    reason: str = ""
    metadata: Metadata = field(default_factory=dict)


def _entry(value: object) -> ScopeEntry:
    if isinstance(value, ScopeEntry):
        return value
    if isinstance(value, str):
        return ScopeEntry(target=value)
    if isinstance(value, dict):
        raw_target = (
            value.get("target")
            or value.get("id")
            or value.get("path")
            or value.get("name")
            or value.get("ref")
            or value.get("value")
            or ""
        )
        return ScopeEntry(
            target=str(raw_target),
            access=ScopeAccess(value.get("access", ScopeAccess.WRITE)),
            role=str(value.get("role", "") or ""),
            layers=dict(value.get("layers", {}) or {}),
            reason=str(value.get("reason", "") or ""),
            metadata=dict(value.get("metadata", {}) or {}),
        )
    return ScopeEntry(target=str(value))


def _entries(values: object) -> tuple[ScopeEntry, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, dict, ScopeEntry)):
        values = (values,)
    return tuple(_entry(value) for value in values)  # type: ignore[union-attr]


@dataclass(frozen=True, slots=True, init=False)
class ItemScope:
    projects: tuple[ScopeEntry, ...] = ()
    files: tuple[ScopeEntry, ...] = ()
    declarations: tuple[ScopeEntry, ...] = ()
    blueprint_nodes: tuple[ScopeEntry, ...] = ()
    references: tuple[ScopeEntry, ...] = ()
    runs: tuple[ScopeEntry, ...] = ()
    inbox_items: tuple[ScopeEntry, ...] = ()
    roadmap_items: tuple[ScopeEntry, ...] = ()
    tasks: tuple[ScopeEntry, ...] = ()

    def __init__(
        self,
        *,
        projects: object = (),
        files: object = (),
        declarations: object = (),
        blueprint_nodes: object = (),
        references: object = (),
        runs: object = (),
        inbox_items: object = (),
        roadmap_items: object = (),
        tasks: object = (),
    ) -> None:
        object.__setattr__(self, "projects", _entries(projects))
        object.__setattr__(self, "files", _entries(files))
        object.__setattr__(self, "declarations", _entries(declarations))
        object.__setattr__(self, "blueprint_nodes", _entries(blueprint_nodes))
        object.__setattr__(self, "references", _entries(references))
        object.__setattr__(self, "runs", _entries(runs))
        object.__setattr__(self, "inbox_items", _entries(inbox_items))
        object.__setattr__(self, "roadmap_items", _entries(roadmap_items))
        object.__setattr__(self, "tasks", _entries(tasks))

    def targets(self, kind: str, *, writable_only: bool = False) -> tuple[str, ...]:
        entries = getattr(self, kind)
        if writable_only:
            entries = tuple(entry for entry in entries if entry.access is ScopeAccess.WRITE)
        return tuple(entry.target for entry in entries)


def scope_from_dict(data: object) -> ItemScope:
    if isinstance(data, ItemScope):
        return data
    raw = data if isinstance(data, dict) else {}
    return ItemScope(
        projects=raw.get("projects", ()),
        files=raw.get("files", ()),
        declarations=raw.get("declarations", ()),
        blueprint_nodes=raw.get("blueprint_nodes", raw.get("blueprint-nodes", ())),
        references=raw.get("references", ()),
        runs=raw.get("runs", ()),
        inbox_items=raw.get("inbox_items", raw.get("inbox-items", ())),
        roadmap_items=raw.get("roadmap_items", raw.get("roadmap-items", ())),
        tasks=raw.get("tasks", ()),
    )


def compact_scope_entry(entry: ScopeEntry) -> object:
    if (
        entry.access is ScopeAccess.WRITE
        and not entry.role
        and not entry.layers
        and not entry.reason
        and not entry.metadata
    ):
        return entry.target
    out: dict[str, Any] = {"target": entry.target}
    if entry.access is not ScopeAccess.WRITE:
        out["access"] = entry.access.value
    if entry.role:
        out["role"] = entry.role
    if entry.layers:
        out["layers"] = entry.layers
    if entry.reason:
        out["reason"] = entry.reason
    if entry.metadata:
        out["metadata"] = entry.metadata
    return out


def compact_scope(scope: ItemScope) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in (
        "projects",
        "files",
        "declarations",
        "blueprint_nodes",
        "references",
        "runs",
        "inbox_items",
        "roadmap_items",
        "tasks",
    ):
        entries = getattr(scope, key)
        if entries:
            out[key] = [compact_scope_entry(entry) for entry in entries]
    return out
