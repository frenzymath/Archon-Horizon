"""Abstract agent interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archon_horizon.core.inbox import InboxItem
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonResult, HorizonTask
from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import Workspace


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
    # Optional cooperative cancellation token supplied by the orchestrator.
    cancel: Any | None = None
    metadata: Metadata = field(default_factory=dict)


class HorizonAgent(ABC):
    """Owns long-horizon formalization work for one task."""

    @abstractmethod
    def run_task(self, context: HorizonContext) -> HorizonResult:
        """Run one fresh Horizon invocation from explicit context."""
