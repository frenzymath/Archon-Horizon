"""Concurrent inbox adds must not collide on the same id (I-0388)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxStatus
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider, InboxLoadError


def test_concurrent_create_allocates_distinct_ids(tmp_path: Path) -> None:
    inbox = FilesystemInboxProvider(tmp_path / "inbox" / "local")
    n = 12
    start = threading.Barrier(n)
    created: list[str] = []
    lock = threading.Lock()

    def worker(k: int) -> None:
        start.wait()  # maximize contention on the id allocation
        item = inbox.create_item(
            InboxDraft(kind=InboxKind.HINT, body=f"item {k}\n\nbody {k}")
        )
        with lock:
            created.append(item.id)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every add got a unique id, and every item survived (no overwrite).
    assert len(set(created)) == n
    stored = {it.id for it in inbox.list_items()}
    assert len(stored) == n
    bodies = {it.body for it in inbox.list_items()}
    assert len(bodies) == n


def test_empty_item_file_does_not_crash_unrelated_load(tmp_path: Path) -> None:
    """A sibling lane mid-write can leave an item file momentarily empty; the
    pure-Python YAML loader decodes that to ``None``. Loading the collection to
    mutate an *unrelated* item must not crash on it (I-0398, I-0621)."""
    inbox = FilesystemInboxProvider(tmp_path / "inbox" / "local")
    keep = inbox.create_item(InboxDraft(kind=InboxKind.ISSUE, body="keep\n\nbody"))
    victim = inbox.create_item(InboxDraft(kind=InboxKind.ISSUE, body="transient\n\nbody"))

    # Simulate the truncate window of a non-atomic writer: the file exists but
    # is empty, so `yaml.load` returns None.
    (tmp_path / "inbox" / "local" / "items" / f"{victim.id}.yaml").write_text("", "utf-8")

    # The empty file is retried and then reported as a *named* corrupt item,
    # never the opaque AttributeError from `inbox_item_from_dict(None)`.
    with pytest.raises(InboxLoadError, match=victim.id):
        inbox.list_items()

    # Once the writer finishes (here: we restore real content), the unrelated
    # item is fully mutable again — no data was lost.
    inbox._save_item(victim)  # noqa: SLF001 — simulate the writer completing
    inbox.update_status(keep.id, InboxStatus.CLOSED)
    assert inbox.get_item(keep.id).status is InboxStatus.CLOSED
    assert inbox.get_item(victim.id).body.startswith("transient")


def test_item_writes_are_atomic(tmp_path: Path) -> None:
    """`_save_item` must swap the finished file in via os.replace so a concurrent
    reader never observes a truncated file — the root cause of the None-parse
    crash. Assert no torn/empty item file is ever visible during a hammer of
    interleaved writes and reads."""
    inbox = FilesystemInboxProvider(tmp_path / "inbox" / "local")
    item = inbox.create_item(InboxDraft(kind=InboxKind.ISSUE, body="x\n\nbody"))
    items_dir = tmp_path / "inbox" / "local" / "items"
    stop = threading.Event()
    seen_empty: list[str] = []

    def writer() -> None:
        for i in range(200):
            inbox.update_body(item.id, f"x\n\nbody {i}")

    def reader() -> None:
        while not stop.is_set():
            for path in items_dir.glob("*.yaml"):
                if path.read_text("utf-8").strip() == "":
                    seen_empty.append(path.name)

    w = threading.Thread(target=writer)
    r = threading.Thread(target=reader)
    r.start()
    w.start()
    w.join()
    stop.set()
    r.join()

    assert seen_empty == []  # atomic replace never exposes an empty file


def test_mutation_does_not_prune_a_concurrently_created_item(tmp_path: Path) -> None:
    """A status/label edit must write only its own item — never rewrite the whole
    collection and delete a sibling item another lane created after we loaded
    (the phantom-deletion family, I-0611). Simulate by snapshotting for the edit,
    creating a NEW item, then completing the edit against the stale snapshot."""
    inbox = FilesystemInboxProvider(tmp_path / "inbox" / "local")
    victim = inbox.create_item(InboxDraft(kind=InboxKind.ISSUE, body="edit me\n\nbody"))

    # Interleave: another lane creates a fresh item mid-edit.
    other = inbox.create_item(InboxDraft(kind=InboxKind.HINT, body="fresh\n\nbody"))

    # A status edit on the first item must not delete the freshly-created one.
    inbox.update_status(victim.id, InboxStatus.CLOSED)
    ids = {it.id for it in inbox.list_items()}
    assert other.id in ids  # survived the unrelated mutation
    assert inbox.get_item(victim.id).status is InboxStatus.CLOSED

    # Deleting one item leaves the other intact.
    inbox.delete_item(victim.id)
    ids = {it.id for it in inbox.list_items()}
    assert ids == {other.id}
