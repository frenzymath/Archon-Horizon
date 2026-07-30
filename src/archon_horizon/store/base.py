"""Abstract persistence contracts.

The orchestrator depends only on these interfaces, never on the filesystem.
Inboxes are deliberately absent: they have their own provider abstraction
(:mod:`archon_horizon.inboxes`). These stores cover the runtime state the
roadmap puts under ``.archon-horizon/``: events, tasks, the structured roadmap,
and the memory file.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from archon_horizon.core.events import Event
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask


class EventLog(ABC):
    @abstractmethod
    def append(self, event: Event) -> None: ...

    @abstractmethod
    def read_all(self) -> list[Event]: ...


class TaskStore(ABC):
    @abstractmethod
    def allocate_id(self) -> str: ...

    @abstractmethod
    def get(self, task_id: str) -> HorizonTask: ...

    @abstractmethod
    def list(self) -> list[HorizonTask]: ...

    @abstractmethod
    def put(self, task: HorizonTask) -> HorizonTask:
        """Persist a task, minting an id when it is empty. Returns the stored task."""

    @abstractmethod
    def delete(self, task_id: str) -> None: ...

    def add_comment(
        self, task_id: str, body: str, author: str | None = None, metadata: dict | None = None
    ) -> None:
        raise NotImplementedError("this task store does not support comments")

    def append_history(self, task_id: str, entry: dict) -> None:
        raise NotImplementedError("this task store does not support history")


class RoadmapStore(ABC):
    @abstractmethod
    def load(self) -> Roadmap: ...

    @abstractmethod
    def save(self, roadmap: Roadmap) -> None: ...

    def add_comment(
        self, item_id: str, body: str, author: str | None = None, metadata: dict | None = None
    ) -> None:
        raise NotImplementedError("this roadmap store does not support comments")

    def append_history(self, item_id: str, entry: dict) -> None:
        raise NotImplementedError("this roadmap store does not support history")


class MemoryStore(ABC):
    @abstractmethod
    def load(self) -> str: ...

    @abstractmethod
    def save(self, text: str) -> None: ...


class RunStore(ABC):
    @abstractmethod
    def allocate_id(self) -> str: ...

    @abstractmethod
    def get(self, run_id: str) -> RunRecord: ...

    @abstractmethod
    def list(self) -> list[RunRecord]: ...

    @abstractmethod
    def put(self, run: RunRecord) -> RunRecord: ...


class ReportStore(ABC):
    @abstractmethod
    def write(self, name: str, text: str) -> str:
        """Write a markdown report and return its workspace-relative ref."""

    @abstractmethod
    def read(self, name: str) -> str: ...

    @abstractmethod
    def list(self) -> list[str]: ...
