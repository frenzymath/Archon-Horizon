"""Per-run, numbered log directories with ordered, nestable sessions.

Each ``archon-horizon run`` claims its own numbered run directory, and every
agent invocation inside it is an ordered, numbered *session*; a session may
nest child sessions for subagents. Two runs launched at the same instant
never collide: a run directory is claimed with an *exclusive* ``mkdir`` (atomic
on POSIX) and the next free number is retried on contention — so concurrency
safety comes from the filesystem, not from a lock we have to hold.

Layout::

    runs/
      0001/
        run.yaml
        sessions/
          0001-ground/                transcript.jsonl  meta.json
          0002-horizon-T-0007/        transcript.jsonl  meta.json
            subagents/
              0001-blueprint-lint/    transcript.jsonl  meta.json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LEADING_NUMBER = re.compile(r"(\d+)")


def _numbered_dirs(parent: Path) -> list[Path]:
    """Return numbered child directories in numeric order.

    The ``0001`` formatting is a minimum width, not a cap; after ``9999`` the
    next directory is ``10000`` and must sort after ``9999``.
    """
    if not parent.exists():
        return []

    def key(path: Path) -> tuple[int, str]:
        match = _LEADING_NUMBER.match(path.name)
        return (int(match.group(1)) if match else -1, path.name)

    return sorted(
        (child for child in parent.iterdir() if child.is_dir() and _LEADING_NUMBER.match(child.name)),
        key=key,
    )


def _max_number(parent: Path) -> int:
    highest = 0
    for child in _numbered_dirs(parent):
        match = _LEADING_NUMBER.match(child.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest


def _claim(parent: Path, label: str = "", *, width: int = 4) -> Path:
    """Atomically claim the next free ``NNNN[-label]`` directory under parent.

    ``width`` is only a minimum display width. The sequence is unbounded.
    """
    parent.mkdir(parents=True, exist_ok=True)
    number = _max_number(parent) + 1
    while True:
        suffix = f"-{label}" if label else ""
        candidate = parent / f"{number:0{width}d}{suffix}"
        try:
            candidate.mkdir(exist_ok=False)
            return candidate
        except FileExistsError:
            number += 1


@dataclass(frozen=True, slots=True)
class SessionLog:
    """One agent invocation's log directory."""

    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def transcript_path(self) -> Path:
        return self.path / "transcript.jsonl"

    @property
    def meta_path(self) -> Path:
        return self.path / "meta.json"

    def write_meta(self, meta: dict[str, Any]) -> None:
        self.meta_path.write_text(json.dumps(meta, indent=2), "utf-8")

    def read_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {}
        text = self.meta_path.read_text("utf-8").strip()
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def new_subsession(self, label: str) -> "SessionLog":
        return SessionLog(_claim(self.path / "subagents", label))

    def subsessions(self) -> list["SessionLog"]:
        sub = self.path / "subagents"
        return [SessionLog(p) for p in _numbered_dirs(sub)]


@dataclass(frozen=True, slots=True)
class RunLog:
    """One run's directory: the run record plus its ordered sessions."""

    path: Path

    @property
    def id(self) -> str:
        return self.path.name

    @property
    def sessions_dir(self) -> Path:
        return self.path / "sessions"

    def new_session(self, label: str) -> SessionLog:
        return SessionLog(_claim(self.sessions_dir, label))

    def sessions(self) -> list[SessionLog]:
        return [SessionLog(p) for p in _numbered_dirs(self.sessions_dir)]


class RunLogTree:
    """The ``runs/`` directory: allocates and looks up numbered run logs."""

    def __init__(self, runs_dir: Path) -> None:
        self._dir = runs_dir

    def allocate(self) -> RunLog:
        return RunLog(_claim(self._dir))

    def get(self, run_id: str) -> RunLog:
        return RunLog(self._dir / run_id)

    def ids(self) -> list[str]:
        return [p.name for p in _numbered_dirs(self._dir)]
