"""Fixed sync-boundary coordinator contract and a default."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from archon_horizon.core.inbox import SyncResult
from archon_horizon.core.sessions import SyncBoundary
from archon_horizon.inboxes.base import InboxProvider


class SyncCoordinator(ABC):
    """Runs deterministic sync steps between agent/tool phases."""

    @abstractmethod
    def sync(self, boundary: SyncBoundary) -> list[SyncResult]:
        """Synchronize all configured providers for a fixed boundary."""


class MultiProviderSyncCoordinator(SyncCoordinator):
    """Sync every configured provider that advertises the ``sync`` capability.

    Boundaries are accepted uniformly; a provider decides internally whether a
    given boundary changes anything. This keeps the boundary set fixed and the
    behavior deterministic, as the roadmap requires.
    """

    def __init__(self, providers: Sequence[InboxProvider]) -> None:
        self._providers = tuple(providers)

    def sync(self, boundary: SyncBoundary) -> list[SyncResult]:
        results: list[SyncResult] = []
        for provider in self._providers:
            if "sync" in provider.capabilities:
                results.append(provider.sync())
        return results

