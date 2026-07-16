"""Abstract agent interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonResult, HorizonTask
from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import Workspace


@dataclass(frozen=True, slots=True)
class HorizonContext:
    """What one automated session needs to launch: identity and plumbing only.

    Workspace state (roadmap, inbox, memory, DAG) is NOT carried here — the
    agent pulls it on demand through the ``horizon`` CLI, as the `horizon`
    skill describes.
    """

    workspace: Workspace
    run: RunRecord
    task: HorizonTask
    log_dir: Path | None = None
    # Which round of the run this session is (0-based) and the run's planned
    # total — exported to the agent's env so it can pace multi-round work.
    round_index: int | None = None
    rounds_total: int | None = None
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
