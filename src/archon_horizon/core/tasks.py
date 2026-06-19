"""Task, proposal, and result contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from .clock import utc_now
from .types import Metadata


class AgentName(StrEnum):
    INFORMAL = "informal"
    HORIZON = "horizon"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProposalStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    APPLIED = "applied"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


@dataclass(frozen=True, slots=True)
class WriteSet:
    files: tuple[str, ...] = ()
    projects: tuple[str, ...] = ()
    workspace: bool = False


@dataclass(frozen=True, slots=True)
class HorizonTask:
    id: str
    project: str
    objective: str
    status: TaskStatus = TaskStatus.QUEUED
    write_set: WriteSet = field(default_factory=WriteSet)
    roadmap_refs: tuple[str, ...] = ()
    inbox_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Proposal:
    id: str
    title: str
    body: str
    status: ProposalStatus = ProposalStatus.DRAFT
    project: str | None = None
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

