"""Write locks for parallel Horizon sessions.

Parallel sessions are allowed only when their declared write sets do not
overlap. An unknown write set (a project named with no specific files) is
treated pessimistically as a whole-project lock, per the roadmap.
"""

from __future__ import annotations

import json
import os
import socket
import time
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from archon_horizon.store import serde
from archon_horizon.core.tasks import WriteSet


def write_sets_conflict(a: WriteSet, b: WriteSet) -> bool:
    """True if two write sets cannot safely run at the same time."""
    if a.workspace or b.workspace:
        return True
    shared_projects = set(a.projects) & set(b.projects)
    if not shared_projects:
        return bool(set(a.files) & set(b.files))
    # A project with no declared files is an unknown write set -> lock the
    # whole project, so any other claim on it conflicts.
    if not a.files or not b.files:
        return True
    return bool(set(a.files) & set(b.files))


class LockManager(ABC):
    @abstractmethod
    def acquire(self, token: str, write_set: WriteSet) -> bool:
        """Reserve ``write_set`` under ``token``. False if it conflicts."""

    @abstractmethod
    def release(self, token: str) -> None: ...


class InMemoryLockManager(LockManager):
    """Process-local locks — enough for one workspace scheduler instance."""

    def __init__(self) -> None:
        self._held: dict[str, WriteSet] = {}
        self._guard = threading.Lock()

    def acquire(self, token: str, write_set: WriteSet) -> bool:
        with self._guard:
            for owner, held in self._held.items():
                if owner != token and write_sets_conflict(held, write_set):
                    return False
            self._held[token] = write_set
            return True

    def release(self, token: str) -> None:
        with self._guard:
            self._held.pop(token, None)


def _process_alive(pid: int, host: str) -> bool:
    if host != socket.gethostname():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FilesystemLockManager(LockManager):
    """Persistent write locks for concurrent ``archon-horizon`` processes.

    Locks are stored as JSON files under ``locks/active``. A short-lived guard
    directory serializes scan-and-claim so two processes cannot both observe a
    conflict-free state and claim overlapping write sets.
    """

    def __init__(self, locks_dir: Path, *, stale_dir_name: str = "stale") -> None:
        self._root = locks_dir
        self._active = locks_dir / "active"
        self._stale = locks_dir / stale_dir_name
        self._guard = locks_dir / ".guard"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._guard.mkdir(exist_ok=False)
                break
            except FileExistsError:
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                self._guard.rmdir()
            except FileNotFoundError:
                pass

    @staticmethod
    def _safe_token(token: str) -> str:
        return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in token)

    def _path(self, token: str) -> Path:
        return self._active / f"{self._safe_token(token)}.json"

    def _read_lock(self, path: Path) -> tuple[str, WriteSet, dict] | None:
        try:
            data = json.loads(path.read_text("utf-8"))
            token = str(data["token"])
            write_set = serde.write_set_from_dict(data.get("write_set", {}))
            return token, write_set, data
        except Exception:
            return None

    def _active_locks(self) -> list[tuple[Path, str, WriteSet, dict]]:
        if not self._active.exists():
            return []
        locks = []
        for path in self._active.glob("*.json"):
            parsed = self._read_lock(path)
            if parsed is None:
                continue
            token, write_set, data = parsed
            pid = int(data.get("pid") or 0)
            host = str(data.get("host") or "")
            if pid and host and not _process_alive(pid, host):
                self._stale.mkdir(parents=True, exist_ok=True)
                path.rename(self._stale / path.name)
                continue
            locks.append((path, token, write_set, data))
        return locks

    def acquire(self, token: str, write_set: WriteSet) -> bool:
        with self._locked():
            self._active.mkdir(parents=True, exist_ok=True)
            for _, owner, held, _ in self._active_locks():
                if owner != token and write_sets_conflict(held, write_set):
                    return False
            payload = {
                "token": token,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "created_at": time.time(),
                "write_set": serde.to_jsonable(write_set),
            }
            self._path(token).write_text(json.dumps(payload, indent=2), "utf-8")
            return True

    def release(self, token: str) -> None:
        with self._locked():
            self._path(token).unlink(missing_ok=True)
