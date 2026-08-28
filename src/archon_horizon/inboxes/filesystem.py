"""The ``local`` inbox: sharded human-editable files under ``inbox/local/``.

Backs the fully-editable local inbox the roadmap describes: agents and the
live dashboard can add hints/issues/info notices, relabel, complete, reject,
archive, and delete drafts. Items live one-per-file in ``items/*.yaml`` and
comments live under ``comments/<item-id>/*.md``. Local items default to
``agent-ready``.
"""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import (
    PARTICIPANTS_KEY,
    READ_BY_KEY,
    InboxDraft,
    InboxFilter,
    InboxItem,
    InboxKind,
    InboxStatus,
    SyncResult,
    conversation_participants,
    is_conversation,
    item_readers,
    matches_filter,
)
from archon_horizon.core.provenance import agent_provenance
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
# Top-level ``status:`` is a scalar on every item file Horizon writes. Peeling it
# off with a line scan lets list filters skip archived/closed bodies (and their
# comment/history trees) without a full YAML round-trip.
_STATUS_LINE_RE = re.compile(r"(?m)^status:\s*(\S+)\s*$")

# Reading an item file can race a concurrent lane's write. Writes are atomic
# (temp + os.replace), so a reader normally sees only complete content; these
# bound a defence-in-depth retry for a residual race (e.g. a legacy non-atomic
# writer mid-upgrade) before a file is treated as genuinely corrupt.
_LOAD_RETRIES = 5
_LOAD_RETRY_SLEEP = 0.02


class InboxLoadError(RuntimeError):
    """A single inbox item file could not be parsed, named so the operator can
    find it — instead of the opaque ``AttributeError`` a ``None`` parse used to
    raise deep inside :func:`serde.inbox_item_from_dict`."""


class FilesystemInboxProvider(InboxProvider):
    capabilities = frozenset({"read", "create", "edit", "delete", "label", "comment", "status", "sync", "read_state"})

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
        actor_name = (actor or "").strip() or "system"
        entry: dict[str, object] = {
            "at": utc_now().isoformat(),
            "actor": actor_name,
            "field": field,
            "from": before,
            "to": after,
            "note": note,
        }
        provenance = agent_provenance()
        if provenance and actor_name.lower() == provenance.get("role"):
            entry["provenance"] = provenance
        append_history(
            self._history_path(item_id),
            entry,
        )

    def _item_paths(self) -> list[Path]:
        if not self._items_dir.exists():
            return []
        return sorted(self._items_dir.glob(f"*.{self._codec.extension}"))

    @staticmethod
    def _status_from_text(text: str) -> str | None:
        match = _STATUS_LINE_RE.search(text)
        return match.group(1) if match else None

    def _read_item_text(self, path: Path) -> tuple[str | None, Exception | str | None]:
        """Return ``(text, error)`` for one item file, or ``(None, None)`` if gone."""
        last: Exception | str | None = None
        for _ in range(_LOAD_RETRIES):
            try:
                return path.read_text("utf-8"), None
            except FileNotFoundError:
                return None, None
            except OSError as exc:
                last = exc
            time.sleep(_LOAD_RETRY_SLEEP)
        return None, last

    def _parse_item_text(self, path: Path, text: str) -> InboxItem | None:
        last: Exception | str | None = None
        for _ in range(_LOAD_RETRIES):
            try:
                data = self._codec.loads(text)
            except Exception as exc:  # a half-written file may not even parse
                last = exc
            else:
                if isinstance(data, dict):
                    return serde.inbox_item_from_dict(data)
                last = f"parsed to {type(data).__name__}, not a mapping"
            # Re-read on parse failure: a concurrent writer may have finished.
            fresh, read_err = self._read_item_text(path)
            if fresh is None:
                if read_err is None:
                    return None
                last = read_err
                break
            text = fresh
            time.sleep(_LOAD_RETRY_SLEEP)
        raise InboxLoadError(f"could not read inbox item {path.name}: {last}")

    def _read_item(self, path: Path) -> InboxItem | None:
        """Parse one item file, tolerating a concurrent writer.

        Item files are written atomically (see :meth:`_save_item`), so a reader
        normally sees only complete content. A sibling lane can still delete a
        file between the directory glob and this read, or — during a mixed-version
        window — leave a truncated file a pure-Python ``yaml.load`` decodes to
        ``None``. Retry a few times, return ``None`` when the file has simply
        vanished (nothing to load), and only raise a *named* error once a file
        stays unparseable — never the opaque ``AttributeError`` a ``None`` parse
        used to trigger, which took down unrelated inbox writes (I-0398, I-0621).
        """
        text, read_err = self._read_item_text(path)
        if text is None:
            if read_err is None:
                return None  # deleted concurrently between glob and read
            raise InboxLoadError(f"could not read inbox item {path.name}: {read_err}")
        return self._parse_item_text(path, text)

    def _hydrate(
        self,
        item: InboxItem,
        *,
        comments: bool = True,
        history: bool = True,
    ) -> InboxItem:
        """Attach sharded comments/history. List filters often skip history."""
        extra: dict[str, object] = {}
        if comments:
            comment_rows = read_comments(self._comment_dir(item.id))
            if comment_rows:
                extra["comments"] = comment_rows
        if history:
            history_rows = read_history(self._history_path(item.id))
            if history_rows:
                extra["history"] = history_rows
        if not extra:
            return item
        return dataclasses.replace(item, metadata={**item.metadata, **extra})

    def _load(
        self,
        *,
        comments: bool = True,
        history: bool = True,
        status: InboxStatus | None = None,
    ) -> dict[str, InboxItem]:
        """Load item files, optionally skipping non-matching statuses early.

        Mature workspaces keep thousands of archived items. A status-filtered
        list only needs the open (or closed) minority, so peek the scalar
        ``status:`` line before YAML-parsing the body and before walking each
        item's comment/history shards.
        """
        items: dict[str, InboxItem] = {}
        wanted = status.value if status is not None else None
        for path in self._item_paths():
            text, read_err = self._read_item_text(path)
            if text is None:
                if read_err is None:
                    continue
                raise InboxLoadError(f"could not read inbox item {path.name}: {read_err}")
            if wanted is not None:
                peeked = self._status_from_text(text)
                if peeked is not None and peeked != wanted:
                    continue
            item = self._parse_item_text(path, text)
            if item is None:
                continue
            if wanted is not None and item.status is not status:
                continue
            items[item.id] = self._hydrate(item, comments=comments, history=history)
        return items

    def _save_item(self, item: InboxItem) -> None:
        self._items_dir.mkdir(parents=True, exist_ok=True)
        stored = dataclasses.replace(item, metadata=without_comments(item.metadata))
        payload = self._codec.dumps(serde.to_jsonable(stored))
        # Write atomically: a plain ``write_text`` truncates the file first, so a
        # concurrent lane's ``_load`` can read it empty (``yaml.load`` -> ``None``)
        # and crash. ``os.replace`` swaps in the finished file in one step, so a
        # reader always sees either the old or the new complete item (I-0398,
        # I-0621). The temp name keeps the ``.tmp`` suffix so it can never match
        # the ``*.<ext>`` load glob.
        path = self._item_path(item.id)
        tmp = path.parent / f"{path.name}.{os.getpid()}.tmp"
        try:
            tmp.write_text(payload, "utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise

    def _next_id(self, items: dict[str, InboxItem] | None = None) -> str:
        """Allocate the next ``I-NNNN`` from filenames (or a preloaded map).

        Create only needs the highest existing id. Scanning stems avoids parsing
        every archived body just to pick ``I-2028``.
        """
        highest = 0
        if items is not None:
            for key in items:
                match = _ID_RE.fullmatch(key)
                if match:
                    highest = max(highest, int(match.group(1)))
        else:
            for path in self._item_paths():
                match = _ID_RE.fullmatch(path.stem)
                if match:
                    highest = max(highest, int(match.group(1)))
        return f"I-{highest + 1:04d}"

    @contextmanager
    def _create_lock(self, timeout: float = 30.0):
        """Serialize id allocation across concurrent ``inbox add`` writers.

        ``create_item`` reads the highest existing id and writes the next one; two
        processes doing that at once both pick the same ``I-NNNN`` and one body is
        lost. An OS ``flock`` (dies with the holder, no stale reclaim) serializes
        the read-allocate-write. Fails OPEN — on a platform without ``fcntl`` or a
        lock timeout it proceeds unlocked rather than refusing the add, so the
        guard can never block writing an item.
        """
        try:
            import fcntl
        except ImportError:
            yield
            return
        self._path.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path / ".create.lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break  # fail open: allocate unlocked rather than error out
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # closing the fd releases the flock

    def _replace(self, item_id: str, **changes: object) -> None:
        # Write ONLY the mutated item, never the whole collection. `_save`
        # rewrites every item and deletes any file not in the snapshot it loaded,
        # so a status/label edit that raced a concurrent lane's `create` used to
        # clobber the freshly-created item. A single-item atomic write touches
        # nothing else — the only deletions happen in `delete_item` (I-0611 family).
        item = self.get_item(item_id)
        self._save_item(dataclasses.replace(item, updated_at=utc_now(), **changes))

    # ── read ────────────────────────────────────────────────────────

    def list_items(self, filters: InboxFilter | None = None) -> list[InboxItem]:
        # Status is the only filter we can apply before parsing: it is a top-level
        # scalar on every file. Push it into the loader so archived bodies and
        # their comment/history trees stay on disk for the common open-only paths
        # (hooks, `inbox list --status open`, agent synchronizer).
        status = filters.status if filters is not None else None
        # History is only required by the dashboard activity feed (unfiltered
        # list). Filtered CLI/hook lists need comments for snippets/query, not
        # the append-only transition log.
        include_history = status is None
        loaded = self._load(
            comments=True,
            history=include_history,
            status=status,
        )
        if filters is None:
            return list(loaded.values())
        # Status already applied; avoid re-checking it on every row.
        rest = dataclasses.replace(filters, status=None) if status is not None else filters
        if (
            rest.provider is None
            and not rest.labels
            and not rest.kinds
            and rest.project is None
            and rest.audience is None
            and not rest.query
            and rest.owner_task is None
            and rest.unread_for is None
        ):
            return list(loaded.values())
        return [item for item in loaded.values() if matches_filter(item, rest)]

    def get_item(self, item_id: str) -> InboxItem:
        path = self._item_path(item_id)
        item = self._read_item(path)
        if item is None:
            raise KeyError(item_id)
        return self._hydrate(item, comments=True, history=True)

    # ── write ───────────────────────────────────────────────────────

    def create_item(self, item: InboxDraft) -> InboxItem:
        # Serialize id allocation + write so concurrent adds don't collide on the
        # same I-NNNN (I-0388). The lock spans only the read-allocate-save.
        with self._create_lock():
            created = InboxItem(
                id=self._next_id(),
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
            # Write only the new item under the lock (id already deduped by the
            # lock); a full `_save` here would prune a sibling lane's item created
            # since our scan.
            self._save_item(created)
        self._record(created.id, created.author, "created", after=created.status.value, note="opened")
        return created

    def update_labels(self, item_id: str, labels: list[str], actor: str | None = None) -> None:
        before = ", ".join(self.get_item(item_id).labels)
        self._replace(item_id, labels=tuple(labels))
        after = ", ".join(labels)
        if before != after:
            self._record(item_id, actor, "label", before=before, after=after)

    def update_status(self, item_id: str, status: InboxStatus, actor: str | None = None) -> None:
        before = self.get_item(item_id).status.value
        self._replace(item_id, status=status)
        if before != status.value:
            self._record(item_id, actor, "status", before=before, after=status.value)

    def update_kind(self, item_id: str, kind: object, actor: str | None = None) -> None:
        before = self.get_item(item_id).kind.value
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
        item = self.get_item(item_id)
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
        # A reply is new information for every other participant. Keep only the
        # sender marked read; otherwise a conversation read once would never
        # become unread again when another team answered it.
        sender_ids: list[str] = []
        provenance = (metadata or {}).get("provenance")
        if isinstance(provenance, dict):
            for key in ("task", "run"):
                value = str(provenance.get(key) or "").strip()
                if value:
                    sender_ids.append(value)
                    break
        if not sender_ids:
            sender = str(author or "local").strip()
            if sender:
                sender_ids.append(sender)
        item_metadata = {**item.metadata, READ_BY_KEY: sender_ids}
        if is_conversation(item):
            participants = list(conversation_participants(item))
            sender_route = ""
            if isinstance(provenance, dict):
                task = str(provenance.get("task") or "").strip()
                run = str(provenance.get("run") or "").strip()
                sender_route = f"task:{task}" if task else (f"run:{run}" if run else "")
            if not sender_route and str(author or "").strip().lower() in {"human", "horizon"}:
                sender_route = str(author).strip().lower()
            if sender_route and sender_route not in participants:
                participants.append(sender_route)
            item_metadata[PARTICIPANTS_KEY] = participants
        self._save_item(dataclasses.replace(
            item, metadata=item_metadata, updated_at=utc_now()
        ))

    def update_comment(self, item_id: str, index: int, body: str, author: str | None = None) -> None:
        item = self.get_item(item_id)
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

    def set_read(self, item_id: str, reader: str, *, read: bool = True, actor: str | None = None) -> None:
        reader = (reader or "").strip()
        if not reader:
            return
        item = self.get_item(item_id)
        readers = list(item_readers(item))
        if read and reader not in readers:
            readers.append(reader)
        elif not read and reader in readers:
            readers = [r for r in readers if r != reader]
        else:
            return  # no change
        self._replace(item_id, metadata={**item.metadata, READ_BY_KEY: readers})
        self._record(item_id, actor or reader, "read_by", after=("read" if read else "unread"))

    def set_owner(self, item_id: str, owner_task: str, actor: str | None = None) -> None:
        """Move an item into a task's inbox (empty ``owner_task`` shares it with all)."""
        from archon_horizon.core.inbox import OWNER_KEY, item_owner

        item = self.get_item(item_id)
        owner = (owner_task or "").strip()
        before = item_owner(item)
        if before == owner:
            return
        metadata = {**item.metadata}
        if owner:
            metadata[OWNER_KEY] = owner
        else:
            metadata.pop(OWNER_KEY, None)
        self._replace(item_id, metadata=metadata)
        self._record(item_id, actor, "owner", before=before or "everyone", after=owner or "everyone")

    def delete_item(self, item_id: str) -> None:
        # Remove only this item's files. A full `_save` would rewrite the whole
        # collection and could prune a concurrently-created sibling.
        self._item_path(item_id).unlink(missing_ok=True)
        shutil.rmtree(self._comment_dir(item_id), ignore_errors=True)
        self._history_path(item_id).unlink(missing_ok=True)

    def sync(self) -> SyncResult:
        # The local files are the source of truth; nothing to import.
        return SyncResult(provider=self.name)
