"""Filesystem-backed stores under ``.archon-horizon/``.

A thin, dependency-light realization of the store contracts. The event log
is always newline-delimited JSON (an append-only log wants line semantics);
tasks and the roadmap go through the configured :class:`Codec`
(JSON by default, YAML when human-editing is wanted).
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
from pathlib import Path

from archon_horizon.core.events import Event
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask
from archon_horizon.core.clock import utc_now
from archon_horizon.inboxes.sharded import (
    append_history,
    next_comment_id,
    read_comments,
    read_history,
    without_comments,
    write_comment,
)
from archon_horizon.runlog import RunLogTree

from . import serde
from .base import (
    EventLog,
    MemoryStore,
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

    @property
    def _items_dir(self) -> Path:
        return self._dir / "items"

    def _path(self, task_id: str) -> Path:
        return self._items_dir / f"{task_id}.{self._codec.extension}"

    def _comments_dir(self, task_id: str) -> Path:
        return self._dir / "comments" / task_id

    def _history_path(self, task_id: str) -> Path:
        return self._dir / "history" / f"{task_id}.jsonl"

    def allocate_id(self) -> str:
        highest = 0
        if self._items_dir.exists():
            for child in self._items_dir.glob(f"*.{self._codec.extension}"):
                match = _ID_RE.search(child.stem)
                if match:
                    highest = max(highest, int(match.group(1)))
        return f"T-{highest + 1:04d}"

    def get(self, task_id: str) -> HorizonTask:
        task = serde.task_from_dict(self._codec.loads(self._path(task_id).read_text("utf-8")))
        extra: dict[str, object] = {}
        comments = read_comments(self._comments_dir(task.id))
        if comments:
            extra["comments"] = comments
        history = read_history(self._history_path(task.id))
        if history:
            extra["history"] = history
        return dataclasses.replace(task, metadata={**task.metadata, **extra}) if extra else task

    def add_comment(
        self, task_id: str, body: str, author: str | None = None, metadata: dict | None = None
    ) -> None:
        directory = self._comments_dir(task_id)
        comment_id = next_comment_id(directory)
        write_comment(
            directory / f"{comment_id}.md",
            {"id": comment_id, "item": task_id, "author": (author or "").strip() or "local",
             "at": utc_now().isoformat(), **dict(metadata or {}), "body": body},
        )

    def append_history(self, task_id: str, entry: dict) -> None:
        append_history(self._history_path(task_id), entry)

    def list(self) -> list[HorizonTask]:
        if not self._items_dir.exists():
            return []
        ids = {p.stem for p in self._items_dir.glob(f"*.{self._codec.extension}")}
        tasks = [self.get(task_id) for task_id in ids]
        return sorted(tasks, key=lambda t: t.id)

    def put(self, task: HorizonTask) -> HorizonTask:
        if not task.id:
            task = dataclasses.replace(task, id=self.allocate_id())
        self._items_dir.mkdir(parents=True, exist_ok=True)
        stored = dataclasses.replace(task, metadata=without_comments(task.metadata))
        self._path(task.id).write_text(self._codec.dumps(serde.to_jsonable(stored)), "utf-8")
        return task

    def delete(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)
        shutil.rmtree(self._comments_dir(task_id), ignore_errors=True)
        self._history_path(task_id).unlink(missing_ok=True)


class FilesystemRoadmapStore(RoadmapStore):
    def __init__(self, directory: Path, codec: Codec | None = None) -> None:
        self._dir = directory
        self._codec = codec or JsonCodec()

    @property
    def _items_dir(self) -> Path:
        return self._dir / "items"

    def _item_path(self, item_id: str) -> Path:
        return self._items_dir / f"{item_id}.{self._codec.extension}"

    def _comments_dir(self, item_id: str) -> Path:
        return self._dir / "comments" / item_id

    def _history_path(self, item_id: str) -> Path:
        return self._dir / "history" / f"{item_id}.jsonl"

    def load(self) -> Roadmap:
        if not self._items_dir.exists():
            return Roadmap()
        items = []
        for path in sorted(self._items_dir.glob(f"*.{self._codec.extension}")):
            item = serde.roadmap_item_from_dict(self._codec.loads(path.read_text("utf-8")))
            extra: dict[str, object] = {}
            comments = read_comments(self._comments_dir(item.id))
            if comments:
                extra["comments"] = comments
            history = read_history(self._history_path(item.id))
            if history:
                extra["history"] = history
            items.append(dataclasses.replace(item, metadata={**item.metadata, **extra}) if extra else item)
        return Roadmap(items=tuple(items))

    def save(self, roadmap: Roadmap) -> None:
        items = tuple(dataclasses.replace(item, metadata=without_comments(item.metadata)) for item in roadmap.items)
        self._items_dir.mkdir(parents=True, exist_ok=True)
        wanted = {item.id for item in items}
        for item in items:
            self._item_path(item.id).write_text(self._codec.dumps(serde.to_jsonable(item)), "utf-8")
        for path in self._items_dir.glob(f"*.{self._codec.extension}"):
            if path.stem not in wanted:
                path.unlink()
                shutil.rmtree(self._comments_dir(path.stem), ignore_errors=True)
                self._history_path(path.stem).unlink(missing_ok=True)

    def add_comment(
        self, item_id: str, body: str, author: str | None = None, metadata: dict | None = None
    ) -> None:
        directory = self._comments_dir(item_id)
        comment_id = next_comment_id(directory)
        write_comment(
            directory / f"{comment_id}.md",
            {"id": comment_id, "item": item_id, "author": (author or "").strip() or "local",
             "at": utc_now().isoformat(), **dict(metadata or {}), "body": body},
        )

    def append_history(self, item_id: str, entry: dict) -> None:
        append_history(self._history_path(item_id), entry)


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
