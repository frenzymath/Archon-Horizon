from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import ClaimedOperation, FencedExecution, JournalFull, Operation, canonical_json


def boot_identity() -> str:
    path = Path("/proc/sys/kernel/random/boot_id")
    if not path.is_file():
        raise RuntimeError("worker leases require the supported Linux boot identity")
    return path.read_text().strip()


class DurableJournal:
    """Bounded SQLite recovery journal with fenced delivery claims.

    The byte budget reserves half for WAL/checkpoint overhead. Payloads and
    protected recovery records are never evicted to satisfy the budget.
    """

    def __init__(self, state_root: Path, *, max_bytes: int = 64 * 1024 * 1024,
                 minimum_free_bytes: int = 16 * 1024 * 1024,
                 max_offline_seconds: float = 7 * 86400,
                 diagnostic_max_bytes: int = 512 * 1024 * 1024,
                 diagnostic_retention_seconds: float = 7 * 86400,
                 failure_diagnostic_retention_seconds: float = 30 * 86400) -> None:
        if not state_root.is_absolute() or max_bytes < 256 * 1024 or minimum_free_bytes < 0:
            raise ValueError("an absolute state_root and valid storage budgets are required")
        if max_offline_seconds <= 0:
            raise ValueError("offline replay horizon must be positive")
        if diagnostic_max_bytes < 2048 or min(diagnostic_retention_seconds, failure_diagnostic_retention_seconds) < 0:
            raise ValueError("invalid diagnostic storage limits")
        state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_root = state_root.resolve()
        self.path = self.state_root / "journal.sqlite3"
        self.max_bytes = max_bytes
        self.minimum_free_bytes = minimum_free_bytes
        self.max_offline_seconds = max_offline_seconds
        self.diagnostic_max_bytes = diagnostic_max_bytes
        self.diagnostic_retention_seconds = diagnostic_retention_seconds
        self.failure_diagnostic_retention_seconds = failure_diagnostic_retention_seconds
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("PRAGMA wal_autocheckpoint=16")
        self._db.execute("PRAGMA journal_size_limit=65536")
        page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
        self._db.execute(f"PRAGMA max_page_count={max_bytes // (2 * page_size)}")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS operations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE, envelope TEXT NOT NULL,
                digest TEXT NOT NULL, created_at REAL NOT NULL,
                destination TEXT NOT NULL DEFAULT 'api',
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                retry_at REAL NOT NULL DEFAULT 0, claim_token TEXT, claim_until REAL,
                acknowledged_at REAL, error TEXT
            );
            CREATE INDEX IF NOT EXISTS operation_delivery ON operations(state,retry_at,sequence);
            CREATE TABLE IF NOT EXISTS execution_state (
                execution_id TEXT PRIMARY KEY, epoch INTEGER NOT NULL,
                boot_id TEXT NOT NULL, deadline REAL NOT NULL,
                status TEXT NOT NULL, checkpoint TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS provider_requests (
                request_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
                epoch INTEGER NOT NULL, input_hash TEXT NOT NULL,
                state TEXT NOT NULL, provider_thread_id TEXT, result TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostic_reservations (
                request_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL,
                epoch INTEGER NOT NULL, reserved_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS diagnostic_tombstones (
                request_id TEXT PRIMARY KEY, pruned_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS claim_attempts (
                request_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', created_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_claim ON claim_attempts(state) WHERE state='pending';
            CREATE TABLE IF NOT EXISTS recovery_health (
                execution_id TEXT PRIMARY KEY, checked_at REAL NOT NULL,
                succeeded_at REAL, error TEXT
            );
        """)
        self._checkpoint_wal()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _checkpoint_wal(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def enqueue(self, operation: Operation, *, destination: str = "api") -> bool:
        if destination not in {"api", "local_git"}:
            raise ValueError("unsupported operation destination")
        envelope = canonical_json(operation.as_dict())
        digest = hashlib.sha256(envelope.encode()).hexdigest()
        try:
            with self._transaction() as db:
                previous = db.execute("SELECT digest,destination FROM operations WHERE operation_id=?",
                                      (operation.operation_id,)).fetchone()
                if previous:
                    if previous[0] != digest or previous[1] != destination:
                        raise ValueError("idempotency key was reused with different content")
                    return False
                if len(envelope.encode()) > min(self.max_bytes // 4, 1024 * 1024):
                    raise JournalFull("operation exceeds payload budget; use a durable artifact")
                page_size = db.execute("PRAGMA page_size").fetchone()[0]
                used_pages = db.execute("PRAGMA page_count").fetchone()[0] - db.execute("PRAGMA freelist_count").fetchone()[0]
                # Leave room for acknowledgements/fencing even when producers stop.
                if used_pages * page_size + len(envelope.encode()) * 2 + max(32768, self.max_bytes // 20) > self.max_bytes // 2:
                    raise JournalFull("journal growth reserve reached; pause producers")
                if shutil.disk_usage(self.state_root).free < self.minimum_free_bytes + len(envelope.encode()) * 2 + 65536:
                    raise JournalFull("reserved disk space is threatened; pause producers")
                db.execute("INSERT INTO operations(operation_id,envelope,digest,created_at,destination) VALUES(?,?,?,?,?)",
                           (operation.operation_id, envelope, digest, time.time(), destination))
        except sqlite3.OperationalError as error:
            if "full" in str(error).lower():
                raise JournalFull("journal budget exhausted; protected records retained") from error
            raise
        self._checkpoint_wal()
        return True

    def claim(self, *, now: float | None = None, claim_seconds: float = 60,
              destination: str = "api") -> ClaimedOperation | None:
        now = time.time() if now is None else now
        with self._transaction() as db:
            db.execute("UPDATE operations SET state='recovery',error='offline replay horizon exceeded',"
                       "claim_token=NULL,claim_until=NULL WHERE state IN ('pending','inflight') AND destination='api' AND created_at<? "
                       "AND json_extract(envelope,'$.kind') NOT IN ('publication_discovered','publication_verified','publication_failed','execution_finished')",
                       (now - self.max_offline_seconds,))
            row = db.execute("SELECT * FROM operations o WHERE destination=? AND ((state='pending' AND retry_at<=?) "
                             "OR (state='inflight' AND claim_until<=?)) "
                             "AND NOT (json_extract(o.envelope,'$.kind')='execution_finished' AND EXISTS ("
                             "SELECT 1 FROM operations p WHERE p.destination='api' AND p.state!='acknowledged' "
                             "AND json_extract(p.envelope,'$.kind')='publication_discovered' "
                             "AND json_extract(p.envelope,'$.execution_id')=json_extract(o.envelope,'$.execution_id'))) "
                             "AND NOT (json_extract(o.envelope,'$.kind')='execution_finished' AND EXISTS ("
                             "SELECT 1 FROM execution_state e WHERE e.execution_id=json_extract(o.envelope,'$.execution_id') "
                             "AND e.epoch=json_extract(o.envelope,'$.epoch') "
                             "AND json_extract(e.checkpoint,'$.recovery_pending')=1)) "
                             "ORDER BY sequence LIMIT 1",
                             (destination, now, now)).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            db.execute("UPDATE operations SET state='inflight',claim_token=?,claim_until=?,attempts=attempts+1 "
                       "WHERE operation_id=?", (token, now + claim_seconds, row["operation_id"]))
            return ClaimedOperation(Operation(**json.loads(row["envelope"])), token, row["attempts"] + 1)

    def settle(self, claimed: ClaimedOperation, state: str, *, retry_at: float = 0,
               error: str | None = None, now: float | None = None) -> None:
        if state not in {"pending", "acknowledged", "blocked", "recovery"}:
            raise ValueError("invalid delivery disposition")
        with self._transaction() as db:
            changed = db.execute("UPDATE operations SET state=?,retry_at=?,error=?,acknowledged_at=?,"
                                 "claim_token=NULL,claim_until=NULL WHERE operation_id=? AND claim_token=?",
                                 (state, retry_at, error, (time.time() if now is None else now) if state == "acknowledged" else None,
                                  claimed.operation.operation_id, claimed.claim_token)).rowcount
            if changed != 1:
                raise FencedExecution("delivery claim was superseded")

    def renew_delivery(self, claimed: ClaimedOperation, *, seconds: float = 180) -> bool:
        with self._transaction() as db:
            return db.execute("UPDATE operations SET claim_until=? WHERE operation_id=? AND claim_token=? "
                              "AND state='inflight'", (time.time() + seconds, claimed.operation.operation_id,
                                                       claimed.claim_token)).rowcount == 1

    def records(self, state: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            query = "SELECT * FROM operations" + (" WHERE state=?" if state else "") + " ORDER BY sequence"
            return [dict(row) for row in self._db.execute(query, (state,) if state else ())]

    def operation(self, operation_id: str) -> Operation | None:
        with self._lock:
            row = self._db.execute("SELECT envelope FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            return Operation(**json.loads(row[0])) if row else None

    def retry_publication(self, operation_id: str, *, note: str | None = None) -> dict[str, Any]:
        if note is not None and not 1 <= len(note.strip()) <= 1000:
            raise ValueError("retry note must contain 1..1000 characters")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM operations WHERE operation_id=? "
                             "AND (destination='local_git' OR (destination='api' AND json_extract(envelope,'$.kind') "
                             "IN ('publication_discovered','publication_verified','publication_failed'))) "
                             "AND state IN ('blocked','recovery')", (operation_id,)).fetchone()
            if row is None:
                raise ValueError("only blocked publication jobs or evidence receipts can be retried")
            error = "retry_requested: " + note.strip() if note is not None else None
            db.execute("UPDATE operations SET state='pending',retry_at=0,error=?,claim_token=NULL,claim_until=NULL "
                       "WHERE operation_id=?", (error, operation_id))
            return {**dict(row), "state": "pending", "retry_at": 0, "error": error}

    def record_recovery_health(self, execution_id: str, error: str | None) -> None:
        now = time.time()
        with self._transaction() as db:
            db.execute("INSERT INTO recovery_health(execution_id,checked_at,succeeded_at,error) VALUES(?,?,?,?) "
                       "ON CONFLICT(execution_id) DO UPDATE SET checked_at=excluded.checked_at,error=excluded.error,"
                       "succeeded_at=COALESCE(excluded.succeeded_at,recovery_health.succeeded_at)",
                       (execution_id, now, now if error is None else None, error))

    def recovery_health(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM recovery_health ORDER BY execution_id")]

    def compact(self, *, acknowledged_before: float) -> int:
        with self._transaction() as db:
            count = db.execute("""DELETE FROM operations WHERE state='acknowledged' AND acknowledged_at<?
                AND NOT (json_extract(envelope,'$.kind')='execution_finished' AND EXISTS (
                    SELECT 1 FROM provider_requests p WHERE p.execution_id=json_extract(envelope,'$.execution_id')
                    AND p.epoch=json_extract(envelope,'$.epoch') AND NOT EXISTS (
                        SELECT 1 FROM diagnostic_tombstones t WHERE t.request_id=p.request_id)))""",
                               (acknowledged_before,)).rowcount
        self._checkpoint_wal()
        return count

    def grant_lease(self, execution_id: str, epoch: int, seconds: float, *,
                    boot_id: str | None = None, monotonic_now: float | None = None) -> None:
        if epoch < 1 or seconds <= 0:
            raise ValueError("invalid lease")
        now = time.monotonic() if monotonic_now is None else monotonic_now
        boot = boot_id or boot_identity()
        with self._transaction() as db:
            old = db.execute("SELECT * FROM execution_state WHERE execution_id=?", (execution_id,)).fetchone()
            if old and (epoch < old["epoch"] or (epoch == old["epoch"] and old["status"] != "running")):
                raise FencedExecution("cannot renew a fenced execution")
            if old and epoch == old["epoch"] and (old["boot_id"] != boot or old["deadline"] <= now):
                raise FencedExecution("expired local ownership requires a new execution epoch")
            db.execute("INSERT INTO execution_state(execution_id,epoch,boot_id,deadline,status) VALUES(?,?,?,?, 'running') "
                       "ON CONFLICT(execution_id) DO UPDATE SET epoch=excluded.epoch,boot_id=excluded.boot_id,"
                       "deadline=excluded.deadline,status='running',"
                       "checkpoint=CASE WHEN excluded.epoch>execution_state.epoch THEN '{}' ELSE execution_state.checkpoint END",
                       (execution_id, epoch, boot, now + seconds))

    def claim_attempt(self, host_id: str, harness_ids: list[str]) -> dict[str, Any]:
        with self._transaction() as db:
            old = db.execute("SELECT * FROM claim_attempts WHERE state='pending'").fetchone()
            if old:
                if old["created_at"] < time.time() - self.max_offline_seconds:
                    raise RuntimeError("Pending claim exceeds replay horizon; reconcile host executions before admitting work")
                return {**dict(old), "payload": json.loads(old["payload"])}
            identifier = str(uuid.uuid4())
            payload = {"host_id": host_id, "harness_ids": sorted(harness_ids)}
            db.execute("INSERT INTO claim_attempts(request_id,payload,created_at) VALUES(?,?,?)",
                       (identifier, canonical_json(payload), time.time()))
            return {"request_id": identifier, "payload": payload}

    def pending_claim(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM claim_attempts WHERE state='pending'").fetchone()
            return {**dict(row), "payload": json.loads(row["payload"])} if row else None

    def abandon_claim(self, request_id: str, note: str) -> None:
        if not isinstance(note, str) or not note.strip() or len(note) > 16000:
            raise ValueError("claim reconciliation requires an operator note")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM claim_attempts WHERE request_id=? AND state='pending'", (request_id,)).fetchone()
            if row is None:
                raise ValueError("claim is no longer pending")
            payload = {**json.loads(row["payload"]), "resolution_note": note}
            db.execute("UPDATE claim_attempts SET state='reconciled',payload=? WHERE request_id=?", (canonical_json(payload), request_id))

    def resolve_claim(self, request_id: str, *, execution_id: str | None = None,
                      epoch: int = 1, seconds: float = 0) -> bool:
        """Store ownership and consume the claim receipt atomically, without its token."""
        with self._transaction() as db:
            attempt = db.execute("SELECT state FROM claim_attempts WHERE request_id=?", (request_id,)).fetchone()
            if attempt is None or attempt["state"] != "pending":
                return False
            fresh = execution_id is not None and db.execute("SELECT 1 FROM execution_state WHERE execution_id=?", (execution_id,)).fetchone() is None
            if fresh:
                db.execute("INSERT INTO execution_state(execution_id,epoch,boot_id,deadline,status) VALUES(?,?,?,?,?)",
                           (execution_id, epoch, boot_identity(), time.monotonic() + max(0, seconds),
                            "running" if seconds > 0 else "lost"))
            db.execute("UPDATE claim_attempts SET state='resolved' WHERE request_id=?", (request_id,))
            db.execute("DELETE FROM claim_attempts WHERE state='resolved' AND request_id NOT IN (SELECT request_id FROM claim_attempts ORDER BY created_at DESC LIMIT 1000)")
            return bool(fresh)

    def assert_lease(self, execution_id: str, epoch: int, *, boot_id: str | None = None,
                     monotonic_now: float | None = None) -> None:
        now = time.monotonic() if monotonic_now is None else monotonic_now
        with self._lock:
            row = self._db.execute("SELECT * FROM execution_state WHERE execution_id=?", (execution_id,)).fetchone()
        if (row is None or row["epoch"] != epoch or row["boot_id"] != (boot_id or boot_identity())
                or row["status"] != "running" or row["deadline"] <= now):
            raise FencedExecution("execution lease is absent, expired, or superseded")

    def checkpoint(self, execution_id: str, epoch: int, value: dict[str, Any]) -> None:
        encoded = canonical_json(value)
        if len(encoded.encode()) > 65536:
            raise ValueError("checkpoint too large")
        with self._transaction() as db:
            changed = db.execute("UPDATE execution_state SET checkpoint=? WHERE execution_id=? AND epoch=?",
                                 (encoded, execution_id, epoch)).rowcount
            if not changed:
                raise FencedExecution("checkpoint belongs to a superseded execution")

    def fence(self, execution_id: str, epoch: int, status: str = "lost") -> None:
        if status not in {"succeeded", "failed", "cancelled", "lost", "yielded"}:
            raise ValueError("invalid execution outcome")
        with self._transaction() as db:
            db.execute("UPDATE execution_state SET status=? WHERE execution_id=? AND epoch=?",
                       (status, execution_id, epoch))

    def executions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM execution_state").fetchall()
            return [{**dict(row), "checkpoint": json.loads(row["checkpoint"])} for row in rows]

    def begin_request(self, request_id: str, execution_id: str, epoch: int, prompt: str) -> bool:
        self.assert_lease(execution_id, epoch)
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        with self._transaction() as db:
            old = db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["input_hash"] != digest or old["execution_id"] != execution_id or old["epoch"] != epoch:
                    raise ValueError("provider request identity reused")
                return False
            db.execute("INSERT INTO provider_requests VALUES(?,?,?,?, 'uncertain',NULL,NULL,?)",
                       (request_id, execution_id, epoch, digest, time.time()))
        return True

    def finish_request(self, request_id: str, *, state: str, provider_thread_id: str | None,
                       result: dict[str, Any]) -> None:
        if state not in {"completed", "failed", "interrupted", "uncertain"}:
            raise ValueError("invalid provider request state")
        with self._transaction() as db:
            db.execute("UPDATE provider_requests SET state=?,provider_thread_id=?,result=? WHERE request_id=?",
                       (state, provider_thread_id, canonical_json(result), request_id))

    def request(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM provider_requests WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return None
            pruned = self._db.execute("SELECT pruned_at FROM diagnostic_tombstones WHERE request_id=?", (request_id,)).fetchone()
            return {**dict(row), "diagnostics_pruned_at": pruned[0] if pruned else None}

    def _diagnostic_usage(self) -> tuple[int, dict[str, int]]:
        sizes: dict[str, int] = {}
        root = self.state_root / "requests"
        if root.is_dir() and not root.is_symlink():
            try:
                directories = tuple(root.iterdir())
            except FileNotFoundError:
                directories = ()
            for directory in directories:
                if directory.is_dir() and not directory.is_symlink():
                    try:
                        paths = tuple(directory.iterdir())
                    except FileNotFoundError:
                        continue
                    total = 0
                    for path in paths:
                        try:
                            if path.is_file() and not path.is_symlink():
                                total += path.stat().st_size
                        except FileNotFoundError:
                            # Lifecycle cursor files are atomically replaced while
                            # the worker accounts for diagnostic usage.
                            continue
                    sizes[directory.name] = total
        reserved = {row["request_id"]: row["reserved_bytes"] for row in self._db.execute(
            "SELECT request_id,reserved_bytes FROM diagnostic_reservations")}
        return sum(max(sizes.get(key, 0), reserved.get(key, 0)) for key in sizes.keys() | reserved.keys()), sizes

    def diagnostic_capacity(self, required_bytes: int) -> bool:
        with self._lock:
            used, _ = self._diagnostic_usage()
            return (used + required_bytes <= self.diagnostic_max_bytes
                    and shutil.disk_usage(self.state_root).free >= self.minimum_free_bytes + required_bytes)

    def reserve_diagnostics(self, request_id: str, execution_id: str, epoch: int, required_bytes: int) -> None:
        with self._transaction() as db:
            if db.execute("SELECT request_id FROM diagnostic_reservations WHERE request_id=?", (request_id,)).fetchone():
                return
            if not self.diagnostic_capacity(required_bytes):
                raise JournalFull("protected request diagnostics exhaust their byte budget; pause admission")
            db.execute("INSERT INTO diagnostic_reservations VALUES(?,?,?,?,?)",
                       (request_id, execution_id, epoch, required_bytes, time.time()))

    def release_diagnostics(self, request_id: str) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM diagnostic_reservations WHERE request_id=?", (request_id,))

    def prune_diagnostics(self, *, now: float | None = None) -> int:
        """Evict only acknowledged terminal logs, retaining all recovery/context data."""
        now = time.time() if now is None else now
        removed = 0
        with self._lock:
            used, _ = self._diagnostic_usage()
            rows = self._db.execute("""SELECT p.*, e.status AS execution_status, e.checkpoint
                FROM provider_requests p JOIN execution_state e
                ON p.execution_id=e.execution_id AND p.epoch=e.epoch
                WHERE p.state IN ('completed','failed','interrupted') AND e.status<>'running'
                AND NOT EXISTS (SELECT 1 FROM diagnostic_tombstones t WHERE t.request_id=p.request_id)
                ORDER BY p.created_at,p.request_id""").fetchall()
            for row in rows:
                if json.loads(row["checkpoint"]).get("recovery_pending"):
                    continue
                operations = self._db.execute("""SELECT state,envelope,acknowledged_at FROM operations
                    WHERE json_extract(envelope,'$.execution_id')=? AND json_extract(envelope,'$.epoch')=?""",
                    (row["execution_id"], row["epoch"])).fetchall()
                if not operations or any(op["state"] != "acknowledged" for op in operations):
                    continue
                receipts = [op["acknowledged_at"] for op in operations
                            if json.loads(op["envelope"])["kind"] == "execution_finished"]
                if not receipts:
                    continue
                retention = self.diagnostic_retention_seconds if row["execution_status"] == "succeeded" else self.failure_diagnostic_retention_seconds
                if max(receipts) > now - retention:
                    continue
                directory = self.state_root / "requests" / row["request_id"]
                if directory.is_symlink() or directory.parent.resolve() != (self.state_root / "requests").resolve():
                    continue
                for name in ("stdout.jsonl", "stderr.log"):
                    path = directory / name
                    if path.is_file() and not path.is_symlink():
                        amount = path.stat().st_size
                        path.unlink()
                        used -= amount
                        removed += amount
                self.release_diagnostics(row["request_id"])
                self._db.execute("INSERT OR IGNORE INTO diagnostic_tombstones VALUES(?,?)", (row["request_id"], now))
        return removed

    def close(self) -> None:
        self._checkpoint_wal()
        self._db.close()
