"""Abstract execution harness — the engine seam.

This is the single boundary across which the system swaps engines (Claude
Code, Codex, agy, ...). Everything engine-specific lives below this line:
agents and orchestration program against :class:`Harness` only and never
branch on an engine name.

A harness is configured once (model, effort, flags, sandbox) from a
``harnesses.<name>`` config block and reused across calls. Per-call inputs
travel in :class:`HarnessRequest`; engine config does not. ``run`` is
stateless by contract: core orchestration must not depend on hidden resume
state inside the engine (see the roadmap's reproducibility rule).
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from archon_horizon.core.types import Metadata


class HarnessCapability(StrEnum):
    STREAMING = "streaming"
    RESUME = "resume"
    MCP = "mcp"
    CANCELLATION = "cancellation"


@runtime_checkable
class CancelToken(Protocol):
    """Cooperative cancellation. Harnesses poll this during long runs."""

    def is_cancelled(self) -> bool: ...


class Cancellation:
    """A concrete, thread-safe :class:`CancelToken`."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True, slots=True)
class Usage:
    """Per-run telemetry — fuels the budget stop-condition."""

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float | None = None

    def __add__(self, other: "Usage") -> "Usage":
        cost: float | None
        if self.cost_usd is None and other.cost_usd is None:
            cost = None
        else:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        return Usage(
            tokens_in=self.tokens_in + other.tokens_in,
            tokens_out=self.tokens_out + other.tokens_out,
            cost_usd=cost,
        )


@dataclass(frozen=True, slots=True)
class HarnessRequest:
    """One fresh invocation.

    ``prompt`` is the whole instruction; ``cwd`` is the project directory the
    engine runs in; ``context_refs`` are read-only files the engine should
    surface; ``artifact_dir`` is where streamed logs and outputs are written
    (those paths come back as ``HarnessResult.artifact_refs``).
    """

    prompt: str
    cwd: Path
    context_refs: tuple[str, ...] = ()
    artifact_dir: Path | None = None
    timeout_s: float | None = None
    idle_timeout_s: float | None = None
    cancel: CancelToken | None = None
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HarnessResult:
    ok: bool
    text: str
    usage: Usage = field(default_factory=Usage)
    artifact_refs: tuple[str, ...] = ()
    metadata: Metadata = field(default_factory=dict)


class Harness(ABC):
    """A backend capable of running one agent invocation."""

    name: str
    capabilities: frozenset[HarnessCapability] = frozenset()

    @abstractmethod
    def run(self, request: HarnessRequest) -> HarnessResult:
        """Run one fresh invocation and return its result."""
