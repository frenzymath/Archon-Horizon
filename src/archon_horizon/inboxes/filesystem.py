"""The ``local`` inbox: a single human-editable file under ``inboxes/``.

Backs the fully-editable local inbox the roadmap describes — agents and the
live dashboard can add hints/issues/proposals, relabel, complete, reject,
archive, and delete drafts. Items live as a list in one YAML (or JSON) file
so a human can hand-edit it. Local items default to ``archon:accept``.

Each mutation rewrites the file. That is fine at inbox scale and keeps the
on-disk form a straightforward, diffable list.
"""

from __future__ import annotations

import re
from pathlib import Path

from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import (
    InboxDraft,
    InboxFilter,
    InboxItem,
    InboxStatus,
    SyncResult,
    matches_filter,
)
from archon_horizon.store import serde
from archon_horizon.store.codec import Codec, YamlCodec

from .base import InboxProvider

_ID_RE = re.compile(r"I-(\d+)")


class FilesystemInboxProvider(InboxProvider):
    capabilities = frozenset({"read", "create", "edit", "delete", "label", "comment", "sync"})

    def __init__(self, path: Path, name: str = "local", codec: Codec | None = None) -> None:
        self.name = name
        self._path = path
        self._codec = codec or YamlCodec()

    # ── persistence ─────────────────────────────────────────────────

    def _load(self) -> dict[str, InboxItem]:
        if not self._path.exists():
            return {}
        data = self._codec.loads(self._path.read_text("utf-8")) or {}
        items = (serde.inbox_item_from_dict(d) for d in data.get("items", []))
        return {item.id: item for item in items}

    def _save(self, items: dict[str, InboxItem]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "items": [serde.to_jsonable(item) for item in items.values()]}
        self._path.write_text(self._codec.dumps(payload), "utf-8")

    def _next_id(self, items: dict[str, InboxItem]) -> str:
        highest = max((int(m.group(1)) for k in items if (m := _ID_RE.fullmatch(k))), default=0)
        return f"I-{highest + 1:04d}"

    def _replace(self, item_id: str, **changes: object) -> None:
        import dataclasses

        items = self._load()
        items[item_id] = dataclasses.replace(items[item_id], updated_at=utc_now(), **changes)
        self._save(items)

    # ── read ────────────────────────────────────────────────────────

    def list_items(self, filters: InboxFilter | None = None) -> list[InboxItem]:
        return [item for item in self._load().values() if matches_filter(item, filters)]

    def get_item(self, item_id: str) -> InboxItem:
        return self._load()[item_id]

    # ── write ───────────────────────────────────────────────────────

    def create_item(self, item: InboxDraft) -> InboxItem:
        items = self._load()
        created = InboxItem(
            id=self._next_id(items),
            provider=self.name,
            kind=item.kind,
            body=item.body,
            labels=item.labels,
            scope=item.scope,
            source_ref=item.source_ref,
            metadata=item.metadata,
        )
        items[created.id] = created
        self._save(items)
        return created

    def update_labels(self, item_id: str, labels: list[str]) -> None:
        self._replace(item_id, labels=tuple(labels))

    def update_status(self, item_id: str, status: InboxStatus) -> None:
        self._replace(item_id, status=status)

    def add_comment(self, item_id: str, body: str) -> None:
        items = self._load()
        item = items[item_id]
        comments = list(item.metadata.get("comments", []))
        comments.append({"body": body, "at": utc_now().isoformat()})
        self._replace(item_id, metadata={**item.metadata, "comments": comments})

    def delete_item(self, item_id: str) -> None:
        items = self._load()
        items.pop(item_id, None)
        self._save(items)

    def sync(self) -> SyncResult:
        # The local file is the source of truth; nothing to import.
        return SyncResult(provider=self.name)
