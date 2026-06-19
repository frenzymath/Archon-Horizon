"""An in-memory inbox provider — reference implementation and test double.

Implements the full read/create/edit surface so the local-dashboard
capabilities can be exercised without touching disk. A filesystem-backed
``local`` provider and a ``gh``-backed ``github`` provider are the two real
implementations this models.
"""

from __future__ import annotations

import dataclasses

from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import (
    InboxDraft,
    InboxFilter,
    InboxItem,
    InboxStatus,
    SyncResult,
    matches_filter,
)

from .base import InboxProvider


class InMemoryInboxProvider(InboxProvider):
    capabilities = frozenset({"read", "create", "edit", "delete", "label", "comment", "sync"})

    def __init__(self, name: str = "local") -> None:
        self.name = name
        self._items: dict[str, InboxItem] = {}
        self._counter = 0

    def list_items(self, filters: InboxFilter | None = None) -> list[InboxItem]:
        return [item for item in self._items.values() if matches_filter(item, filters)]

    def get_item(self, item_id: str) -> InboxItem:
        return self._items[item_id]

    def create_item(self, item: InboxDraft) -> InboxItem:
        self._counter += 1
        created = InboxItem(
            id=f"I-{self._counter:04d}",
            provider=self.name,
            kind=item.kind,
            body=item.body,
            labels=item.labels,
            scope=item.scope,
            source_ref=item.source_ref,
            metadata=item.metadata,
        )
        self._items[created.id] = created
        return created

    def update_labels(self, item_id: str, labels: list[str]) -> None:
        self._items[item_id] = dataclasses.replace(
            self._items[item_id], labels=tuple(labels), updated_at=utc_now()
        )

    def update_status(self, item_id: str, status: InboxStatus) -> None:
        self._items[item_id] = dataclasses.replace(
            self._items[item_id], status=status, updated_at=utc_now()
        )

    def delete_item(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def sync(self) -> SyncResult:
        return SyncResult(provider=self.name)
