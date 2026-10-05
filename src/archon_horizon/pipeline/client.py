"""Agent API client with persisted request identity and bounded retry.

Pending intents survive an interrupted CLI process; credentials never do.
Replaying an intent still requires the current execution's live authority.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid4, uuid5
from urllib.parse import urlsplit

import httpx
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from .intent_reconciliation import INTENT_REPAIR_PREFIX, repair_command


RETRYABLE_OPERATION_CODES = {
    "review_capacity_unavailable", "review_identity_conflict", "provider_overloaded",
    "rate_limited", "database_unavailable", "connection_error", "timeout",
}
MAX_REQUEST_BYTES = 1024**2


class RetryDeferred(RuntimeError):
    """The server kept an intent durable but asked the client to retry later."""


def response_error_code(response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error") if isinstance(body, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def retryable_operation(response) -> bool:
    return response.status_code in (408, 425, 429) or response.status_code >= 500 or response_error_code(response) in RETRYABLE_OPERATION_CODES


def contract_rejection(body):
    error = body.get("error") if isinstance(body, dict) else None
    return isinstance(error, dict) and error.get("code") in {
        "validation_failed", "invalid_json", "invalid_arguments", "unknown_command", "unknown_record",
        # These named Horizon rejections are raised before committing the
        # requested mutation. Do not generalize this to all 404/409 responses.
        "not_found", "delegator_required", "dependency_cycle", "review_gate_blocked",
        "incomplete_initial_discussion_read", "unread_messages",
        "self_delegation", "unavailable_owner", "open_children", "child_budget_exhausted",
        "review_resolutions_required", "review_plan_incomplete",
        "review_resolution_independence", "review_resolution_invalid",
    }


def invalid_request(response):
    """Only known no-effect rejections can leave the recovery queue automatically."""
    if response.status_code not in (404, 405, 409, 422):
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    if contract_rejection(body):
        return True
    # FastAPI validates path/query arguments and rejects unmatched routes before dispatch.
    detail = body.get("detail")
    return ((response.status_code == 422 and isinstance(detail, list) and bool(detail)
             and all(isinstance(item, dict) and "loc" in item and "type" in item for item in detail))
            or (response.status_code == 404 and detail == "Not Found")
            or (response.status_code == 405 and detail == "Method Not Allowed"))


class AgentClient:
    def __init__(self, base_url: str, token: str, execution_id: str, state_root: Path,
                 *, client: httpx.Client | None = None, max_offline_seconds: int = 7 * 86400,
                 max_journal_bytes: int = 64 * 1024**2, assignment_id: str | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise ValueError("API URL must be an HTTP(S) origin without credentials")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1", "testserver"):
            raise ValueError("remote agent API connections require HTTPS")
        if not state_root.is_absolute() or state_root == Path("/") or ".." in state_root.parts:
            raise ValueError("agent state must use an explicit absolute directory")
        if max_offline_seconds < 1 or max_journal_bytes < 1024**2:
            raise ValueError("agent journal limits must be positive")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.execution_id = execution_id
        self.max_offline_seconds = max_offline_seconds
        self.max_journal_bytes = max_journal_bytes
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = state_root / "api-intents.sqlite3"
        self.client = client or httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False)
        self.owned = client is None
        with self.connect() as db:
            # Parallel CLI calls must inspect and migrate the schema under the
            # same write lock; connect() already supplies a bounded busy timeout.
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS intent (id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, fingerprint TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, response TEXT, created_at REAL NOT NULL)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(intent)").fetchall()}
            if "attempts" not in columns:
                db.execute("ALTER TABLE intent ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "retry_at" not in columns:
                db.execute("ALTER TABLE intent ADD COLUMN retry_at REAL")
            if "resolution" not in columns:
                db.execute("ALTER TABLE intent ADD COLUMN resolution TEXT")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            if assignment_id:
                owner = db.execute("SELECT value FROM metadata WHERE key='assignment_id'").fetchone()
                if owner and owner[0] != assignment_id:
                    raise ValueError("agent intent journal belongs to another assignment")
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('assignment_id', ?)", (assignment_id,))
            # Older clients blocked on syntax errors. Retain their evidence without
            # requiring a new provider turn to reconcile an operation that never ran.
            for row in db.execute("SELECT id,response FROM intent WHERE status='rejected'").fetchall():
                try:
                    rejected = json.loads(row["response"] or "null")
                except ValueError:
                    continue
                if contract_rejection(rejected):
                    db.execute("UPDATE intent SET status='invalid' WHERE id=? AND status='rejected'", (row["id"],))
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self, *, timeout=10):
        db = sqlite3.connect(self.path, timeout=timeout)
        try:
            # Switching a new journal to WAL can return SQLITE_BUSY without
            # invoking SQLite's busy handler during concurrent first access.
            deadline = time.monotonic() + timeout
            while True:
                try:
                    db.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as error:
                    remaining = deadline - time.monotonic()
                    if (getattr(error, "sqlite_errorcode", 0) & 0xff) != sqlite3.SQLITE_BUSY or remaining <= 0:
                        raise
                    time.sleep(min(0.01, remaining))
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA journal_size_limit=65536")
            size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={self.max_journal_bytes // size}")
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def request(self, method: str, path: str, body=None, *, key: str | None = None,
                force_retry: bool = False):
        method = method.upper()
        if method not in ("GET", "POST", "PATCH") or not path.startswith("/api/v3/") or ".." in path.split("/"):
            raise ValueError("agent requests are limited to the versioned Horizon API")
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds the agent journal payload limit")
        fingerprint = hashlib.sha256((method + "\n" + path + "\n" + encoded).encode()).hexdigest()
        if method != "GET":
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT * FROM intent WHERE fingerprint=? AND status='pending' ORDER BY created_at LIMIT 1",
                                      (fingerprint,)).fetchone()
                if key is None and existing:
                    key = existing["id"]
                key = key or str(uuid4())
                previous = db.execute("SELECT * FROM intent WHERE id=?", (key,)).fetchone()
                if previous and previous["fingerprint"] != fingerprint:
                    raise ValueError("idempotency key belongs to another operation")
                if previous and previous["created_at"] < time.time() - self.max_offline_seconds:
                    raise RuntimeError(f"Request {key} exceeds the automatic replay window; reconcile its outcome before creating another intent")
                if (previous and previous["status"] == "pending" and previous["retry_at"]
                        and previous["retry_at"] > time.time() and not force_retry):
                    raise RetryDeferred(f"Request {key} is scheduled for retry at {previous['retry_at']:.0f}")
                db.execute("DELETE FROM intent WHERE status IN ('completed','resolved','invalid') AND id NOT IN (SELECT id FROM intent ORDER BY created_at DESC LIMIT 1000)")
                pending_bytes = db.execute("SELECT coalesce(sum(length(body)),0) FROM intent WHERE status IN ('pending','rejected')").fetchone()[0]
                if not previous and pending_bytes + len(encoded) > 16 * 1024**2:
                    raise RuntimeError("agent intent journal is full; checkpoint instead of losing pending work")
                db.execute("INSERT OR IGNORE INTO intent (id,execution_id,fingerprint,method,path,body,status,response,created_at,attempts,retry_at) VALUES (?,?,?,?,?,?,'pending',NULL,?,0,NULL)",
                           (key, self.execution_id, fingerprint, method, path, encoded, time.time()))
        headers = {"Authorization": f"Bearer {self.token}"}
        if key:
            headers["Idempotency-Key"] = key
        response = None
        for attempt in Retrying(stop=stop_after_attempt(3), wait=wait_random_exponential(multiplier=0.2, max=2),
                                retry=retry_if_exception_type(httpx.TransportError), reraise=True):
            with attempt:
                response = self.client.request(method, self.base_url + path, json=body if method != "GET" else None,
                                               headers=headers)
        assert response is not None
        # Every mutating response must settle the local intent journal.  A
        # server-side failure is still a durable pending intent with a retry
        # deadline; leaving 5xx responses untouched makes replay spin without
        # backoff and can duplicate pressure on an already unhealthy API.
        if method != "GET":
            with self.connect() as db:
                if response.is_success:
                    status, retry_at = "completed", None
                elif invalid_request(response):
                    status, retry_at = "invalid", None
                elif retryable_operation(response):
                    previous = db.execute("SELECT attempts FROM intent WHERE id=?", (key,)).fetchone()
                    attempts = (previous[0] if previous else 0) + 1
                    delay = min(300, 2 ** min(attempts, 8))
                    status, retry_at = "pending", time.time() + delay
                    db.execute("UPDATE intent SET attempts=?,retry_at=? WHERE id=?", (attempts, retry_at, key))
                else:
                    status, retry_at = "rejected", None
                db.execute("UPDATE intent SET status=?,response=?,retry_at=? WHERE id=?",
                           (status, None if response.is_success else response.text[:2000], retry_at, key))
                # Completed results are reconstructible at the API; pending intents are never age-evicted.
                db.execute("DELETE FROM intent WHERE status IN ('completed','resolved','invalid') AND created_at < ?", (time.time() - 7 * 86400,))
        if not response.is_success:
            message = f"Horizon {response.status_code}: {response.text[:2000]} (request key: {key})"
            if retryable_operation(response):
                raise RetryDeferred(message)
            raise RuntimeError(message)
        return response.json()

    def pending(self, *, limit: int = 100):
        if not 1 <= limit <= 100:
            raise ValueError("pending intent page size must be between 1 and 100")
        with self.connect() as db:
            rows = db.execute("SELECT id,execution_id,method,path,body,created_at,status,response,attempts,retry_at FROM intent WHERE status IN ('pending','rejected') ORDER BY created_at,id LIMIT ?", (limit,)).fetchall()
        return [{**dict(row), "body": json.loads(row["body"]),
                 "recovery_command": repair_command(row["id"])} for row in rows]

    def control_notices(self, consumer_id: str) -> dict | None:
        """Best-effort CLI briefing; never journal, acknowledge, or affect a write."""
        key = "control_notices:" + hashlib.sha256(consumer_id.encode()).hexdigest()
        now = time.time()
        try:
            with self.connect(timeout=0.05) as db:
                db.execute("BEGIN IMMEDIATE")
                owner = db.execute("SELECT value FROM metadata WHERE key='assignment_id'").fetchone()
                if not owner:
                    return None
                previous = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
                state = json.loads(previous[0]) if previous else {}
                if 0 <= now - state.get("checked_at", 0) < 30:
                    return None
                state["checked_at"] = now
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(state)))
                db.execute("DELETE FROM metadata WHERE key LIKE 'control_notices:%' AND key NOT IN "
                           "(SELECT key FROM metadata WHERE key LIKE 'control_notices:%' ORDER BY rowid DESC LIMIT 64)")
            content = bytearray()
            with self.client.stream("GET", self.base_url + f"/api/v3/assignments/{owner[0]}/control-notices",
                    headers={"Authorization": f"Bearer {self.token}"}, timeout=httpx.Timeout(2, connect=1)) as response:
                if not response.is_success:
                    return None
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 16384:
                        return None
            summary = json.loads(content)
            if (not isinstance(summary, dict) or type(summary.get("pending")) is not int
                    or not 0 <= summary["pending"] or not isinstance(summary.get("notices"), list)
                    or len(summary["notices"]) > 8
                    or len(json.dumps(summary, ensure_ascii=False).encode()) > 8192):
                return None
            seen = state.get("seen", {})
            fresh = []
            for notice in summary["notices"]:
                if (not isinstance(notice, dict) or not isinstance(notice.get("id"), str)
                        or type(notice.get("revision")) is not int):
                    return None
                old = seen.get(notice["id"], {})
                if old.get("revision") != notice["revision"] or not 0 <= now - old.get("shown_at", 0) < 300:
                    fresh.append(notice)
                    seen[notice["id"]] = {"revision": notice["revision"], "shown_at": now}
            # Retain only the bounded current summary. A changed revision or a
            # still-unhandled notice after five minutes can be surfaced again.
            state["seen"] = {notice["id"]: seen[notice["id"]] for notice in summary["notices"]}
            with self.connect(timeout=0.05) as db:
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, json.dumps(state)))
            if fresh:
                return {**summary, "notices": fresh, "already_shown": len(summary["notices"]) - len(fresh)}
        except Exception:
            # Optional notice delivery must not replace the original operation's
            # result, even if local metadata or the notice response is malformed.
            return None
        return None

    def replay_pending(self, *, limit: int = 20):
        completed, blocked, deferred = [], [], []
        for item in self.pending(limit=limit):
            if item["status"] == "rejected":
                blocked.append({"id": item["id"], "message": "Inspect the authoritative outcome, then run "
                    + repair_command(item["id"]) + ". Rejection: " + (item["response"] or "unknown rejection")})
                continue
            try:
                # A resumed execution is an explicit recovery boundary.  It
                # may retry a deferred intent immediately; the original
                # execution has already consumed the failed attempt.
                resumed = item["execution_id"] != self.execution_id
                self.request(item["method"], item["path"], item["body"], key=item["id"], force_retry=resumed)
                completed.append(item["id"])
            except RetryDeferred as error:
                deferred.append({"id": item["id"], "message": str(error)})
                continue
            except (httpx.HTTPError, RuntimeError, ValueError) as error:
                blocked.append({"id": item["id"], "message": str(error)})
                continue
        with self.connect() as db:
            unresolved = {row[0] for row in db.execute("SELECT id FROM intent WHERE status IN ('pending','rejected')")}
        # A replay can conclusively reject an earlier uncertain request. Keep
        # its diagnostic, but do not report a blocker after it became terminal.
        blocked = [item for item in blocked if item['id'] in unresolved]
        deferred = [item for item in deferred if item['id'] in unresolved]
        result = {"completed": completed, "blocked": blocked, "pending": len(unresolved)}
        if deferred:
            result["deferred"] = deferred
        return result

    def recovery_state(self):
        """Observe one host recovery boundary, retaining identity across executions."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id,status FROM intent WHERE status IN ('pending','rejected') ORDER BY created_at,id").fetchall()
            if not rows:
                db.execute("DELETE FROM metadata WHERE key='intent_recovery'")
                return {"pending": 0, "fingerprint": None, "consecutive_observations": 0, "intents": []}
            fingerprint = hashlib.sha256(json.dumps([list(row) for row in rows]).encode()).hexdigest()
            previous = db.execute("SELECT value FROM metadata WHERE key='intent_recovery'").fetchone()
            previous = json.loads(previous[0]) if previous else {}
            count = 1
            # Adding another rejected request must not rejuvenate the oldest
            # unresolved write. Only its actual resolution starts a new budget.
            if previous.get("oldest_intent_id") == rows[0]['id']:
                count = previous["consecutive_observations"] + (previous["execution_id"] != self.execution_id)
            state = {"fingerprint": fingerprint, "consecutive_observations": count,
                     "execution_id": self.execution_id, "oldest_intent_id": rows[0]['id']}
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('intent_recovery', ?)", (json.dumps(state),))
        return {"pending": len(rows), **{key: state[key] for key in ("fingerprint", "consecutive_observations")},
                "intents": [dict(row) for row in rows[:20]]}

    def resolve_intent(self, key: str, note: str):
        if not note.strip() or len(note) > 8192:
            raise ValueError("intent repair needs a nonempty explanation of at most 8192 characters")
        with self.connect() as db:
            item = db.execute("SELECT * FROM intent WHERE id=?", (key,)).fetchone()
            owner = db.execute("SELECT value FROM metadata WHERE key='assignment_id'").fetchone()
        if not owner or not item or item["status"] not in ("pending", "rejected", "resolved"):
            raise ValueError("intent is not an unresolved request of this assignment")
        assignment_id = owner[0]
        self.request("GET", f"/api/v3/assignments/{assignment_id}/context")
        if item['status'] == 'resolved':
            resolution = json.loads(item['resolution'] or item['response'])
            return {'id': key, 'status': 'resolved', 'obligation_id': resolution['obligation_id']}
        obligation = self.request("POST", "/api/v3/records/obligation", {
            "assignment_id": assignment_id, "kind": "decision",
            "description": f"{INTENT_REPAIR_PREFIX}{key}: {item['method']} {item['path']} (originating execution {item['execution_id']}).",
        }, key=str(uuid5(NAMESPACE_URL, f"horizon:intent:{key}:repair-obligation")), force_retry=True)
        disposition_key = str(uuid5(NAMESPACE_URL, f"horizon:intent:{key}:repair-disposition"))
        disposition = {
            "expected_revision": obligation["revision"], "status": "done",
            "resolution": {"kind": "completed", "note": note, "evidence": []},
        }
        with self.connect() as db:
            previous = db.execute('SELECT body FROM intent WHERE id=?', (disposition_key,)).fetchone()
        if previous:
            # A lost repair acknowledgement must replay its original payload,
            # even if the resumed session paraphrases the explanatory note.
            disposition = json.loads(previous['body'])
        resolved = self.request("POST", f"/api/v3/obligations/{obligation['id']}/resolve", disposition,
            key=disposition_key, force_retry=True)
        with self.connect() as db:
            db.execute("UPDATE intent SET status='resolved',resolution=? WHERE id=? AND status IN ('pending','rejected')",
                (json.dumps({"obligation_id": resolved["id"], "note": disposition['resolution']['note'],
                             "resolved_by_execution_id": self.execution_id}), key))
        return {"id": key, "status": "resolved", "obligation_id": resolved["id"]}

    def close(self):
        if self.owned:
            self.client.close()
