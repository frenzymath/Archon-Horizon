"""Write locks for parallel Horizon sessions.

Parallel sessions are allowed only when their declared write sets do not
overlap. An unknown write set (a project named with no specific files) is
treated pessimistically as a whole-project lock, per the roadmap.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

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
