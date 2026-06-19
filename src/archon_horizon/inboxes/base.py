"""Abstract inbox provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from archon_horizon.core.inbox import InboxDraft, InboxFilter, InboxItem, InboxStatus, SyncResult


class InboxProvider(ABC):
    """Provider-agnostic interface for local and GitHub-like inboxes."""

    name: str
    capabilities: frozenset[str] = frozenset({"read", "sync"})

    @abstractmethod
    def list_items(self, filters: InboxFilter | None = None) -> list[InboxItem]:
        """Return provider items matching filters."""

    @abstractmethod
    def get_item(self, item_id: str) -> InboxItem:
        """Return one item by provider-local id."""

    def create_item(self, item: InboxDraft) -> InboxItem:
        raise NotImplementedError(f"{self.name} does not support creating inbox items")

    def update_labels(self, item_id: str, labels: list[str]) -> None:
        raise NotImplementedError(f"{self.name} does not support label edits")

    def update_status(self, item_id: str, status: InboxStatus) -> None:
        raise NotImplementedError(f"{self.name} does not support status edits")

    def add_comment(self, item_id: str, body: str) -> None:
        raise NotImplementedError(f"{self.name} does not support comments")

    def delete_item(self, item_id: str) -> None:
        raise NotImplementedError(f"{self.name} does not support deletion")

    @abstractmethod
    def sync(self) -> SyncResult:
        """Synchronize provider state and return an import/update summary."""

