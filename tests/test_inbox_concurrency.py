"""Concurrent inbox adds must not collide on the same id (I-0388)."""

from __future__ import annotations

import threading
from pathlib import Path

from archon_horizon.core.inbox import InboxDraft, InboxKind
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider


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
