"""Abstract agent interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from archon_horizon.core.inbox import InboxItem
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonResult, HorizonTask
from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import Workspace


@dataclass(frozen=True, slots=True)
class GroundContext:
    workspace: Workspace
    run: RunRecord
    focus: Focus
    roadmap: Roadmap
    accepted_inbox: tuple[InboxItem, ...] = ()
    memory: str = ""
    blueprint_summary: str = ""
    write_domain: tuple[str, ...] = ()
    previous_report_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    log_dir: Path | None = None
    # When resuming, the native engine session id of the interrupted Ground run.
    resume_session_id: str | None = None
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroundUpdate:
    """The Ground agent's machine-relevant output.

    The agent now mutates roadmap/memory/blueprints on disk and the inbox via the
    CLI *during* its run, so there is no structured payload to apply — only the
    human report. The orchestrator reconstructs what changed by reading state
    back from disk after the session.
    """

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
    write_domain: tuple[str, ...] = ()
    previous_report_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    log_dir: Path | None = None
    # When resuming, the native engine session id of the interrupted Horizon run.
    resume_session_id: str | None = None
    metadata: Metadata = field(default_factory=dict)


class GroundAgent(ABC):
    """Maintains human-facing state and creates work for Horizon."""

    @abstractmethod
    def run_round(self, context: GroundContext) -> GroundUpdate:
        """Run one fresh Ground invocation from explicit context."""

    @abstractmethod
    def handle_horizon_result(
        self,
        context: GroundContext,
        result: HorizonResult,
    ) -> GroundUpdate:
        """Translate Horizon output into roadmap, reports, issues, or tasks."""


class HorizonAgent(ABC):
    """Owns long-horizon formalization work for one task."""

    @abstractmethod
    def run_task(self, context: HorizonContext) -> HorizonResult:
        """Run one fresh Horizon invocation from explicit context."""
