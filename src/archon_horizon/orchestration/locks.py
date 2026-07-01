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
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from archon_horizon.store import serde
from archon_horizon.core.tasks import WriteSet


def write_sets_conflict(a: WriteSet, b: WriteSet) -> bool:
    """True if two write sets cannot safely run at the same time."""
    if a.workspace or b.workspace:
        return True
    if set(a.declarations) & set(b.declarations):
        return True
    if set(a.blueprint_nodes) & set(b.blueprint_nodes):
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


class RunLockHeld(RuntimeError):
    """Raised when another live orchestrator already owns the workspace run lock.

    Two orchestrator processes on one workspace would both mutate the shared
    roadmap, blueprints, and memory — clobbering each other and corrupting the
    YAML (the "duplicate Ground" hazard). Raised only under ``exclusive=True``;
    advisory callers warn and proceed instead (see :func:`workspace_run_lock`).
    """


@dataclass(frozen=True, slots=True)
class RunLockStatus:
    """Outcome of acquiring a workspace run lock.

    ``owned`` is True when we created (or stole a stale) lock and therefore must
    release it on exit. ``concurrent`` holds the live foreign holder's metadata
    when an advisory acquire proceeded alongside another run.
    """

    owned: bool
    concurrent: dict | None = None


@contextmanager
def workspace_run_lock(
    path: Path, *, run_id: str = "", exclusive: bool = True
) -> Iterator[RunLockStatus]:
    """Hold a process-aware lock on a workspace for one run.

    The lock is a single file created with ``O_EXCL`` (atomic). If it already
    exists we inspect the holder: a *live* process under ``exclusive=True`` means
    we refuse (raising :class:`RunLockHeld`); under ``exclusive=False`` we leave
    its lock untouched and proceed anyway, reporting it via
    ``RunLockStatus.concurrent`` so the caller can warn. A dead holder's lock is
    stale and gets stolen regardless. The file is removed on exit only if we own
    it, so a crash leaves a stale — not a poisoned — lock the next run reclaims.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"pid": os.getpid(), "host": socket.gethostname(), "run_id": run_id, "created_at": time.time()}
    )
    owned = False
    concurrent: dict | None = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_run_lock(path)
            if holder is not None and _process_alive(int(holder.get("pid") or 0), str(holder.get("host") or "")):
                if exclusive:
                    raise RunLockHeld(
                        f"another horizon run is active on this workspace "
                        f"(pid {holder.get('pid')} on {holder.get('host')}, run {holder.get('run_id') or '?'}); "
                        "refusing to start a second — it would clobber the shared roadmap/blueprints. "
                        "Wait for it to finish, or remove the stale lock at "
                        f"{path} if that process is gone."
                    )
                # Advisory: another live run owns the lock; proceed beside it.
                concurrent = holder
                break
            # Holder is dead (or unreadable): steal the stale lock and retry.
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        owned = True
        break
    try:
        yield RunLockStatus(owned=owned, concurrent=concurrent)
    finally:
        if not owned:
            return
        holder = _read_run_lock(path)
        if holder is not None and int(holder.get("pid") or 0) == os.getpid():
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _read_run_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


def live_run_lock(path: Path) -> dict | None:
    """Return the run-lock holder's metadata iff a *live* process still owns it.

    The dashboard uses this to tell a genuinely-active run from one whose
    process died without writing a clean end — the latter would otherwise show a
    perpetual "running" spinner. Returns ``None`` when there is no lock or its
    holder is gone.
    """
    holder = _read_run_lock(path)
    if holder is None:
        return None
    pid = int(holder.get("pid") or 0)
    host = str(holder.get("host") or "")
    if pid and _process_alive(pid, host):
        return holder
    return None


class FilesystemLockManager(LockManager):
    """Persistent write locks for concurrent ``archon-horizon`` processes.

    Locks are stored as JSON files under ``locks/active``. A short-lived guard
    directory serializes scan-and-claim so two processes cannot both observe a
    conflict-free state and claim overlapping write sets.
    """

    # A guard older than this (and whose owner is gone, or whose age we cannot
    # otherwise bound) is considered abandoned and stolen. The critical section
    # it protects is a fast scan-and-write, so a few seconds is generous.
    _GUARD_STALE_SECONDS = 15.0

    def __init__(self, locks_dir: Path, *, stale_dir_name: str = "stale") -> None:
        self._root = locks_dir
        self._active = locks_dir / "active"
        self._stale = locks_dir / stale_dir_name
        self._guard = locks_dir / ".guard"
        self._guard_owner = self._guard / "owner.json"

    def _guard_is_abandoned(self) -> bool:
        """True if the held guard should be stolen: its owner process is dead, or
        it is older than the stale window (covering a crash that left no readable
        owner, or a dead holder on another host)."""
        owner = _read_run_lock(self._guard_owner)
        if owner is not None:
            pid, host = int(owner.get("pid") or 0), str(owner.get("host") or "")
            if pid and host == socket.gethostname() and not _process_alive(pid, host):
                return True  # local owner is gone
            created = owner.get("created_at")
            if isinstance(created, (int, float)):
                return (time.time() - created) > self._GUARD_STALE_SECONDS
        # No readable owner marker: fall back to the directory's own age.
        try:
            return (time.time() - self._guard.stat().st_mtime) > self._GUARD_STALE_SECONDS
        except FileNotFoundError:
            return False

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self._root.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self._guard.mkdir(exist_ok=False)
                break
            except FileExistsError:
                # A crash between mkdir and rmdir would otherwise deadlock every
                # future acquire/release; steal an abandoned guard instead of
                # spinning forever.
                if self._guard_is_abandoned():
                    try:
                        self._guard_owner.unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        self._guard.rmdir()
                    except (FileNotFoundError, OSError):
                        pass
                    continue
                time.sleep(0.05)
        try:
            self._guard_owner.write_text(
                json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "created_at": time.time()}),
                "utf-8",
            )
        except OSError:
            pass
        try:
            yield
        finally:
            try:
                self._guard_owner.unlink()
            except FileNotFoundError:
                pass
            try:
                self._guard.rmdir()
            except (FileNotFoundError, OSError):
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
