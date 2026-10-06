"""Recover bounded activity from retained stdout without interrupting providers.

This bridge only adds telemetry to the existing durable delivery journal. It
never submits a provider request, extends a lease, or replays usage accounting.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import UUID, uuid5

from ..activity_display import display_event
from ..worker_config import WorkerConfig, open_journal
from .contracts import Operation
from .journal import JournalFull


def replay_once(config, journal, offsets, *, limit=200):
    """Return enqueued count; commit each cursor only after durable enqueue."""
    adapters = {str(h.id): h.adapter.removesuffix("_exec") for h in config.harnesses}
    with sqlite3.connect(f"file:{journal.path}?mode=ro", uri=True) as reader:
        reader.row_factory = sqlite3.Row
        requests = reader.execute("""SELECT r.*, e.checkpoint FROM provider_requests r
            JOIN execution_state e ON r.execution_id=e.execution_id
            ORDER BY r.created_at DESC LIMIT 1000""").fetchall()
    emitted, scanned = 0, 0
    for request in requests:
        checkpoint = json.loads(request["checkpoint"])
        adapter = adapters.get(checkpoint.get("harness_id"))
        thread = checkpoint.get("provider_thread_record_id")
        if not adapter or not thread:
            continue
        request_id = str(UUID(request["request_id"]))
        path = journal.state_root / "requests" / request_id / "stdout.jsonl"
        if path.parent.is_symlink():
            continue
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        saved = offsets.execute("SELECT offset FROM activity_cursor WHERE request_id=?", (request_id,)).fetchone()
        position = saved[0] if saved else 0
        with os.fdopen(fd, "rb") as stream:
            stream.seek(position)
            # Bound both useful events and ignored/invalid input per pass.
            for _ in range(500):
                start = stream.tell()
                line = stream.readline(1024 * 1024 + 1)
                scanned += len(line)
                if not line or (len(line) <= 1024 * 1024 and not line.endswith(b"\n")):
                    break
                position = stream.tell()
                try:
                    raw = json.loads(line) if len(line) <= 1024 * 1024 else None
                except (ValueError, UnicodeDecodeError):
                    raw = None
                display = display_event(adapter, raw)
                if display:
                    operation_id = str(uuid5(UUID(request_id), f"activity-replay:{start}"))
                    # Retain the first durable observation, including its time,
                    # after a crash between enqueue and the cursor commit.
                    if journal.operation(operation_id) is None:
                        started_at = datetime.fromtimestamp(request["created_at"], timezone.utc).isoformat()
                        display["detail"] = ("Provider log recovered/observed by Horizon at the displayed time; "
                            "original event time unavailable. Request started " + started_at + ".\n\n" + display["detail"])
                        operation = Operation.create(request["execution_id"], request["epoch"], "provider_observed", {
                            "event": "native_event", "request_id": request_id, "provider_thread_record_id": thread,
                            "adapter": adapter, "raw": {"type": "horizon.activity", "horizon_activity": display}},
                            operation_id=operation_id)
                        if journal.enqueue(operation):
                            emitted += 1
                offsets.execute("INSERT INTO activity_cursor VALUES(?,?) ON CONFLICT(request_id) DO UPDATE SET offset=excluded.offset",
                                (request_id, position))
                offsets.commit()
                if emitted >= limit or scanned >= 8 * 1024 * 1024:
                    return emitted
    return emitted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-config", type=Path, required=True)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    config = WorkerConfig.model_validate_json(args.worker_config.read_text())
    with open(config.journal_root / "activity-replay.lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        journal = open_journal(config)
        cursor_path = config.journal_root / "activity-replay.sqlite3"
        offsets = sqlite3.connect(cursor_path)
        os.chmod(cursor_path, 0o600)
        offsets.execute("CREATE TABLE IF NOT EXISTS activity_cursor(request_id TEXT PRIMARY KEY, offset INTEGER NOT NULL)")
        try:
            while True:
                try:
                    count = replay_once(config, journal, offsets)
                    print(json.dumps({"enqueued": count}), flush=True)
                except JournalFull:
                    print(json.dumps({"status": "paused_for_journal_capacity"}), flush=True)
                if not args.follow:
                    break
                time.sleep(5)
        finally:
            offsets.close()
            journal.close()


if __name__ == "__main__":
    main()
