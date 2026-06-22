"""Abstract agent interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from archon_horizon.core.inbox import InboxDraft, InboxItem
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonResult, HorizonTask, Proposal
from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import Workspace


@dataclass(frozen=True, slots=True)
class InformalContext:
    workspace: Workspace
    run: RunRecord
    focus: Focus
    roadmap: Roadmap
    accepted_inbox: tuple[InboxItem, ...] = ()
    memory: str = ""
    blueprint_summary: str = ""
    previous_report_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    log_dir: Path | None = None
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InformalUpdate:
    roadmap: Roadmap | None = None
    memory: str | None = None
    tasks: tuple[HorizonTask, ...] = ()
    proposals: tuple[Proposal, ...] = ()
    local_issues: tuple[InboxDraft, ...] = ()
    report: str = ""
    artifact_refs: tuple[str, ...] = ()
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HorizonContext:
    workspace: Workspace
    run: RunRecord
    task: HorizonTask
    roadmap: Roadmap
    accepted_inbox: tuple[InboxItem, ...] = ()
    memory: str = ""
    previous_report_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    log_dir: Path | None = None
    metadata: Metadata = field(default_factory=dict)


class InformalAgent(ABC):
    """Maintains human-facing state and creates work for Horizon."""

    @abstractmethod
    def run_round(self, context: InformalContext) -> InformalUpdate:
        """Run one fresh informal invocation from explicit context."""

    @abstractmethod
    def handle_horizon_result(
        self,
        context: InformalContext,
        result: HorizonResult,
    ) -> InformalUpdate:
        """Translate Horizon output into roadmap, reports, issues, or tasks."""


class HorizonAgent(ABC):
    """Owns long-horizon formalization work for one task."""

    @abstractmethod
    def run_task(self, context: HorizonContext) -> HorizonResult:
        """Run one fresh Horizon invocation from explicit context."""

