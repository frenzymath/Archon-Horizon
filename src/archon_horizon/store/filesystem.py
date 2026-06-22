"""Filesystem-backed stores under ``.archon-horizon/``.

A thin, dependency-light realization of the store contracts. The event log
is always newline-delimited JSON (an append-only log wants line semantics);
tasks, proposals, and the roadmap go through the configured :class:`Codec`
(JSON by default, YAML when human-editing is wanted).
"""

from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from archon_horizon.core.events import Event
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask, Proposal
from archon_horizon.runlog import RunLogTree

from . import serde
from .base import (
    EventLog,
    MemoryStore,
    ProposalStore,
    ReportStore,
    RoadmapStore,
    RunStore,
    TaskStore,
)
from .codec import Codec, JsonCodec

_ID_RE = re.compile(r"-(\d+)\b")


def _next_numbered_id(directory: Path, prefix: str, width: int = 4) -> str:
    highest = 0
    if directory.exists():
        for child in directory.iterdir():
            match = _ID_RE.search(child.stem)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{prefix}-{highest + 1:0{width}d}"


class FilesystemEventLog(EventLog):
    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: Event) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(serde.to_jsonable(event), ensure_ascii=False) + "\n")

    def read_all(self) -> list[Event]:
        if not self._path.exists():
            return []
        events: list[Event] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                events.append(serde.event_from_dict(json.loads(line)))
        return events


class FilesystemTaskStore(TaskStore):
    def __init__(self, directory: Path, codec: Codec | None = None) -> None:
        self._dir = directory
        self._codec = codec or JsonCodec()

    def _path(self, task_id: str) -> Path:
        return self._dir / f"{task_id}.{self._codec.extension}"

    def allocate_id(self) -> str:
        return _next_numbered_id(self._dir, "T")

    def get(self, task_id: str) -> HorizonTask:
        return serde.task_from_dict(self._codec.loads(self._path(task_id).read_text("utf-8")))

    def list(self) -> list[HorizonTask]:
        if not self._dir.exists():
            return []
        tasks = [
            serde.task_from_dict(self._codec.loads(p.read_text("utf-8")))
            for p in self._dir.glob(f"*.{self._codec.extension}")
        ]
        return sorted(tasks, key=lambda t: t.id)

    def put(self, task: HorizonTask) -> HorizonTask:
        if not task.id:
            task = dataclasses.replace(task, id=self.allocate_id())
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(task.id).write_text(self._codec.dumps(serde.to_jsonable(task)), "utf-8")
        return task


class FilesystemProposalStore(ProposalStore):
    def __init__(self, directory: Path, codec: Codec | None = None) -> None:
        self._dir = directory
        self._codec = codec or JsonCodec()

    def _path(self, proposal_id: str) -> Path:
        return self._dir / f"{proposal_id}.{self._codec.extension}"

    def allocate_id(self) -> str:
        return _next_numbered_id(self._dir, "P")

    def get(self, proposal_id: str) -> Proposal:
        return serde.proposal_from_dict(self._codec.loads(self._path(proposal_id).read_text("utf-8")))

    def list(self) -> list[Proposal]:
        if not self._dir.exists():
            return []
        proposals = [
            serde.proposal_from_dict(self._codec.loads(p.read_text("utf-8")))
            for p in self._dir.glob(f"*.{self._codec.extension}")
        ]
        return sorted(proposals, key=lambda p: p.id)

    def put(self, proposal: Proposal) -> Proposal:
        if not proposal.id:
            proposal = dataclasses.replace(proposal, id=self.allocate_id())
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path(proposal.id).write_text(self._codec.dumps(serde.to_jsonable(proposal)), "utf-8")
        return proposal


class FilesystemRoadmapStore(RoadmapStore):
    def __init__(self, path: Path, codec: Codec | None = None) -> None:
        self._path = path
        self._codec = codec or JsonCodec()

    def load(self) -> Roadmap:
        if not self._path.exists():
            return Roadmap()
        return serde.roadmap_from_dict(self._codec.loads(self._path.read_text("utf-8")))

    def save(self, roadmap: Roadmap) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(self._codec.dumps(serde.to_jsonable(roadmap)), "utf-8")


class FilesystemMemoryStore(MemoryStore):
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str:
        return self._path.read_text("utf-8") if self._path.exists() else ""

    def save(self, text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text, "utf-8")


class FilesystemRunStore(RunStore):
    """Run records as ``runs/<id>/run.<ext>`` — the record lives inside the
    run's own log directory, alongside its sessions."""

    def __init__(self, tree: RunLogTree, codec: Codec | None = None) -> None:
        self._tree = tree
        self._codec = codec or JsonCodec()

    def _path(self, run_id: str) -> Path:
        return self._tree.get(run_id).path / f"run.{self._codec.extension}"

    def allocate_id(self) -> str:
        return self._tree.allocate().id

    def get(self, run_id: str) -> RunRecord:
        return serde.run_record_from_dict(self._codec.loads(self._path(run_id).read_text("utf-8")))

    def list(self) -> list[RunRecord]:
        records = []
        for run_id in self._tree.ids():
            path = self._path(run_id)
            if path.exists():
                records.append(serde.run_record_from_dict(self._codec.loads(path.read_text("utf-8"))))
        return records

    def put(self, run: RunRecord) -> RunRecord:
        if not run.id:
            run = dataclasses.replace(run, id=self._tree.allocate().id)
        path = self._path(run.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._codec.dumps(serde.to_jsonable(run)), "utf-8")
        return run


class FilesystemReportStore(ReportStore):
    """Markdown reports under ``reports/``; refs are workspace-relative posix."""

    def __init__(self, directory: Path, state_path: Path) -> None:
        self._dir = directory
        self._state_path = state_path

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.md"

    def write(self, name: str, text: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        path.write_text(text, "utf-8")
        return path.relative_to(self._state_path.parent).as_posix()

    def read(self, name: str) -> str:
        return self._path(name).read_text("utf-8")

    def list(self) -> list[str]:
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.md"))
