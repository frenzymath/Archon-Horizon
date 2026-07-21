"""The ``local`` inbox: sharded human-editable files under ``inbox/local/``.

Backs the fully-editable local inbox the roadmap describes: agents and the
live dashboard can add hints/issues/info notices, relabel, complete, reject,
archive, and delete drafts. Items live one-per-file in ``items/*.yaml`` and
comments live under ``comments/<item-id>/*.md``. Local items default to
``agent-ready``.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
from pathlib import Path

from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import (
    InboxDraft,
    InboxFilter,
    InboxItem,
    InboxKind,
    InboxStatus,
    SyncResult,
    matches_filter,
)
from archon_horizon.store import serde
from archon_horizon.store.codec import Codec, YamlCodec

from .base import InboxProvider
from .sharded import (
    append_history,
    next_comment_id,
    read_comments,
    read_history,
    without_comments,
    write_comment,
)

_ID_RE = re.compile(r"I-(\d+)")


class FilesystemInboxProvider(InboxProvider):
    capabilities = frozenset({"read", "create", "edit", "delete", "label", "comment", "status", "sync"})

    def __init__(self, root: Path, name: str = "local", codec: Codec | None = None) -> None:
        self.name = name
        self._path = root
        self._codec = codec or YamlCodec()

    # ── persistence ─────────────────────────────────────────────────

    @property
    def _items_dir(self) -> Path:
        return self._path / "items"

    @property
    def _comments_dir(self) -> Path:
        return self._path / "comments"

    @property
    def _history_dir(self) -> Path:
        return self._path / "history"

    def _item_path(self, item_id: str) -> Path:
        return self._items_dir / f"{item_id}.{self._codec.extension}"

    def _comment_dir(self, item_id: str) -> Path:
        return self._comments_dir / item_id

    def _history_path(self, item_id: str) -> Path:
        return self._history_dir / f"{item_id}.jsonl"

    def _record(self, item_id: str, actor: str | None, field: str, *, before: str = "", after: str = "", note: str = "") -> None:
        append_history(
            self._history_path(item_id),
            {
                "at": utc_now().isoformat(),
                "actor": (actor or "").strip() or "system",
                "field": field,
                "from": before,
                "to": after,
                "note": note,
            },
        )

    def _load(self) -> dict[str, InboxItem]:
        if not self._items_dir.exists():
            return {}
        items: dict[str, InboxItem] = {}
        for path in sorted(self._items_dir.glob(f"*.{self._codec.extension}")):
            item = serde.inbox_item_from_dict(self._codec.loads(path.read_text("utf-8")))
            extra: dict[str, object] = {}
            comments = read_comments(self._comment_dir(item.id))
            if comments:
                extra["comments"] = comments
            history = read_history(self._history_path(item.id))
            if history:
                extra["history"] = history
            if extra:
                item = dataclasses.replace(item, metadata={**item.metadata, **extra})
            items[item.id] = item
        return items

    def _save(self, items: dict[str, InboxItem]) -> None:
        self._items_dir.mkdir(parents=True, exist_ok=True)
        wanted = set(items)
        for item in items.values():
            self._save_item(item)
        for path in self._items_dir.glob(f"*.{self._codec.extension}"):
            if path.stem not in wanted:
                path.unlink()
                shutil.rmtree(self._comment_dir(path.stem), ignore_errors=True)
                self._history_path(path.stem).unlink(missing_ok=True)

    def _save_item(self, item: InboxItem) -> None:
        self._items_dir.mkdir(parents=True, exist_ok=True)
        stored = dataclasses.replace(item, metadata=without_comments(item.metadata))
        self._item_path(item.id).write_text(self._codec.dumps(serde.to_jsonable(stored)), "utf-8")

    def _next_id(self, items: dict[str, InboxItem]) -> str:
        highest = max((int(m.group(1)) for k in items if (m := _ID_RE.fullmatch(k))), default=0)
        return f"I-{highest + 1:04d}"

    def _replace(self, item_id: str, **changes: object) -> None:
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
            audience=item.audience,
            author=item.author,
            source_ref=item.source_ref,
            metadata=item.metadata,
        )
        items[created.id] = created
        self._save(items)
        self._record(created.id, created.author, "created", after=created.status.value, note="opened")
        return created

    def update_labels(self, item_id: str, labels: list[str], actor: str | None = None) -> None:
        before = ", ".join(self._load()[item_id].labels)
        self._replace(item_id, labels=tuple(labels))
        after = ", ".join(labels)
        if before != after:
            self._record(item_id, actor, "label", before=before, after=after)

    def update_status(self, item_id: str, status: InboxStatus, actor: str | None = None) -> None:
        before = self._load()[item_id].status.value
        self._replace(item_id, status=status)
        if before != status.value:
            self._record(item_id, actor, "status", before=before, after=status.value)

    def update_kind(self, item_id: str, kind: object, actor: str | None = None) -> None:
        before = self._load()[item_id].kind.value
        self._replace(item_id, kind=InboxKind(kind))
        if before != str(kind):
            self._record(item_id, actor, "kind", before=before, after=str(kind))

    def update_body(self, item_id: str, body: str, actor: str | None = None) -> None:
        self._replace(item_id, body=body)
        self._record(item_id, actor, "body", note="description edited")

    def add_comment(
        self,
        item_id: str,
        body: str,
        author: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        items = self._load()
        item = items[item_id]
        now = utc_now().isoformat()
        comment_id = next_comment_id(self._comment_dir(item_id))
        write_comment(
            self._comment_dir(item_id) / f"{comment_id}.md",
            {
                "id": comment_id,
                "item": item_id,
                "author": author or "local",
                "at": now,
                **dict(metadata or {}),
                "body": body,
            },
        )
        self._save_item(dataclasses.replace(item, updated_at=utc_now()))

    def update_comment(self, item_id: str, index: int, body: str, author: str | None = None) -> None:
        items = self._load()
        item = items[item_id]
        comments = list(item.metadata.get("comments", []))
        if index < 0 or index >= len(comments):
            raise IndexError(f"comment index {index} out of range for {item_id}")
        current = dict(comments[index])
        current["body"] = body
        if author is not None:
            current["author"] = author or "local"
        current["edited_at"] = utc_now().isoformat()
        comment_id = str(current.get("id") or f"C-{index + 1:04d}")
        write_comment(self._comment_dir(item_id) / f"{comment_id}.md", current)
        self._save_item(dataclasses.replace(item, updated_at=utc_now()))

    def delete_item(self, item_id: str) -> None:
        items = self._load()
        items.pop(item_id, None)
        self._save(items)

    def sync(self) -> SyncResult:
        # The local files are the source of truth; nothing to import.
        return SyncResult(provider=self.name)
