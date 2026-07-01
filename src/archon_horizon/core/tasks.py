"""Task and result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .clock import utc_now
from .scope import ItemScope
from .types import Metadata


class AgentName(StrEnum):
    GROUND = "ground"
    HORIZON = "horizon"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class WriteSet:
    files: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    declarations: tuple[str, ...] = ()
    blueprint_nodes: tuple[str, ...] = ()
    workspace: bool = False


@dataclass(frozen=True, slots=True)
class HorizonTask:
    id: str
    project: str
    objective: str
    title: str = ""
    explanation: str = ""
    projects: tuple[str, ...] = ()
    priority: str = "normal"
    status: TaskStatus = TaskStatus.QUEUED
    write_set: WriteSet = field(default_factory=WriteSet)
    scope: ItemScope = field(default_factory=ItemScope)
    roadmap_refs: tuple[str, ...] = ()
    inbox_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HorizonResult:
    task_id: str
    status: TaskStatus
    report: str
    artifact_refs: tuple[str, ...] = ()
    local_issue_refs: tuple[str, ...] = ()
    metadata: Metadata = field(default_factory=dict)
