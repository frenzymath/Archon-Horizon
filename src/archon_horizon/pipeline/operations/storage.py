"""Conservative disk pressure, owned diagnostics cleanup, and consistent backups."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager
import hashlib
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url

from ..errors import DomainError
from ..persistence.records import canonical, transaction_lock
from ..persistence.schema import tables


@contextmanager
def service_logging(config):
    """Rotate only this service's files; do not change the host journal policy."""
    budget = config.storage.service_log_budget_bytes
    if budget == 0:
        yield
        return
    if budget < 4096:
        raise ValueError("service_log_budget_bytes must be zero or at least 4096")
    root = config.state_root / "logs"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "service.log"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    chunk = budget // 4
    class BoundedHandler(RotatingFileHandler):
        def format(self, record):
            message = super().format(record).encode("utf-8")
            limit = min(65536, chunk // 2)
            if len(message) > limit:
                return message[:max(0, limit - 40)].decode("utf-8", errors="ignore") + " [diagnostic record truncated]"
            return message.decode("utf-8")

        def shouldRollover(self, record):
            if self.stream is None:
                self.stream = self._open()
            return self.stream.tell() + len((self.format(record) + "\n").encode("utf-8")) > self.maxBytes
    handler = BoundedHandler(path, maxBytes=chunk, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s request=%(request_id)s "
        "trace=%(trace_id)s route=%(http_route)s status=%(http_status_code)s duration_ms=%(duration_ms)s",
        defaults={"request_id": "-", "trace_id": "-", "http_route": "-",
                  "http_status_code": "-", "duration_ms": "-"},
    ))
    previous = []
    for name in ("archon_horizon.pipeline", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        previous.append((logger, logger.handlers[:], logger.propagate, logger.level))
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO)
    try:
        yield
    finally:
        for logger, handlers, propagate, level in previous:
            logger.handlers, logger.propagate, logger.level = handlers, propagate, level
        handler.close()


def pressure(root: Path, policy) -> dict:
    ancestor = root
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    usage = shutil.disk_usage(ancestor)
    used_percent = 100 * usage.used / usage.total
    # The percentage is a cleanup target.  It must never turn ordinary work
    # into a pause; only the explicit byte floor and emergency usage threshold
    # are admission guards.
    cleanup_target_bytes = math.ceil(usage.total * policy.cleanup_target_free_percent / 100)
    required_free_bytes = policy.minimum_free_bytes
    status = "pause" if usage.free < required_free_bytes or used_percent >= policy.pause_used_percent else (
        "warn" if used_percent >= policy.warn_used_percent else "ready")
    return {"status": status, "free_bytes": usage.free, "total_bytes": usage.total,
            "required_free_bytes": required_free_bytes,
            "cleanup_target_free_percent": policy.cleanup_target_free_percent,
            "cleanup_target_bytes": cleanup_target_bytes,
            "cleanup_recommended": usage.free < cleanup_target_bytes,
            "used_percent": round(used_percent, 2), "observed_at": datetime.now(timezone.utc).isoformat()}


class StorageManager:
    def __init__(self, config):
        self.root = config.state_root
        self.policy = config.storage

    def cleanup_preview(self, *, now: float | None = None) -> dict:
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        diagnostics = self.root / "diagnostics"
        eligible = []
        # Only explicitly terminal/unpinned diagnostic directories are reclaimable.
        # Provider state, journal, artifacts and arbitrary workspaces never enter here.
        if diagnostics.is_dir() and not diagnostics.is_symlink():
            for directory in diagnostics.iterdir():
                marker = directory / "retention.json"
                if directory.is_symlink() or not directory.is_dir() or not marker.is_file() or marker.is_symlink():
                    continue
                try:
                    record = json.loads(marker.read_bytes())
                    if not isinstance(record, dict):
                        continue
                    required = {"schema_version", "terminal_at", "failure_resolved", "pinned"}
                    if record.get("schema_version") == 2:
                        required.add("outcome")
                    if set(record) != required or record["schema_version"] not in (1, 2) or record["pinned"] is not False:
                        continue
                    terminal = datetime.fromisoformat(record["terminal_at"])
                    if terminal.tzinfo is None or record["failure_resolved"] is not True:
                        continue
                    outcome = record.get("outcome", "failed")
                    if outcome not in ("succeeded", "failed", "cancelled"):
                        continue
                    retention = (self.policy.diagnostic_retention_seconds if outcome == "succeeded"
                                 else self.policy.failure_diagnostic_retention_seconds)
                    files = list(directory.rglob("*"))
                    if any(path.is_symlink() for path in files):
                        continue
                    size = sum(path.stat().st_size for path in files if path.is_file())
                    eligible.append({"path": str(directory), "bytes": size, "kind": "diagnostic",
                                     "terminal_at": terminal.timestamp(), "expired": now - terminal.timestamp() >= retention,
                                     "marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest()})
                except (OSError, ValueError, TypeError):
                    continue
        remaining = sum(item["bytes"] for item in eligible)
        candidates = []
        for item in sorted(eligible, key=lambda item: item["terminal_at"]):
            if item["expired"] or remaining > self.policy.diagnostic_budget_bytes:
                candidates.append({**item, "reason": "retention" if item["expired"] else "diagnostic_budget"})
                remaining -= item["bytes"]
        backups = self.backup_preview()
        candidates.extend(backups["candidates"])
        return {"observed_at": datetime.now(timezone.utc).isoformat(), "candidates": candidates,
                "reclaimable_bytes": sum(item["bytes"] for item in candidates),
                "retained_diagnostic_bytes": remaining, "backups": backups,
                "protected": ["artifacts", "journals", "provider state", "workspaces", "unresolved failures", "newest verified backup"]}

    def backup_preview(self):
        root = self.root / "backups"
        verified = []
        if root.is_dir() and not root.is_symlink():
            for directory in root.iterdir():
                if not directory.is_dir() or directory.is_symlink() or directory.name.startswith("."):
                    continue
                try:
                    files = list(directory.rglob("*"))
                    if any(path.is_symlink() for path in files):
                        continue
                    manifest = verify_backup(directory)
                    verified.append({"path": str(directory), "bytes": sum(path.stat().st_size for path in files if path.is_file()),
                        "created_at": manifest["created_at"], "kind": "backup", "reason": "backup_budget",
                        "marker_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()})
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        ordered = sorted(verified, key=lambda item: item["created_at"])
        remaining = sum(item["bytes"] for item in ordered)
        count, candidates = len(ordered), []
        # The newest verified set remains even when it alone exceeds the budget.
        for item in ordered[:-1]:
            if count > self.policy.backup_keep_count or remaining > self.policy.backup_budget_bytes:
                candidates.append(item)
                remaining -= item["bytes"]
                count -= 1
        return {"candidates": candidates, "verified_count": len(ordered), "retained_bytes": remaining,
                "over_budget": remaining > self.policy.backup_budget_bytes,
                "newest_verified": ordered[-1]["path"] if ordered else None}

    def cleanup(self, preview: dict) -> dict:
        # Recompute eligibility under an exclusive ownership lock and compare exact markers.
        import fcntl
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        removed = []
        with (self.root / "cleanup.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = {item["path"]: item for item in self.cleanup_preview()["candidates"]}
            for requested in preview.get("candidates", []):
                item = current.get(requested.get("path"))
                if not item or item["marker_sha256"] != requested.get("marker_sha256"):
                    continue
                source = Path(item["path"])
                if source.parent not in (self.root / "diagnostics", self.root / "backups"):
                    continue
                retired = source.with_name(".retired-" + uuid4().hex)
                source.rename(retired)
                shutil.rmtree(retired)
                removed.append(item)
        return {"removed": removed, "freed_bytes": sum(item["bytes"] for item in removed)}


def database_retention_preview(conn, config, *, limit: int = 500) -> dict:
    """Bounded transport retention; semantic history and recovery references stay pinned."""
    if not 1 <= limit <= 1000:
        raise ValueError("retention batch size must be between 1 and 1000")
    now = conn.execute(select(func.now())).scalar_one()
    request, principal, execution = (tables[name] for name in ("api_request", "principal", "execution"))
    active_agent = select(principal.c.id).join(execution, principal.c.execution_id == execution.c.id).where(
        principal.c.id == request.c.principal_id, execution.c.status.in_(("starting", "running", "stopping"))).exists()
    requests = conn.execute(select(request.c.id, request.c.revision).where(
        request.c.status.in_(("completed", "failed")), request.c.expires_at < now,
        request.c.operation.not_in(("bootstrap_project", "launch_run")),
        request.c.created_at < now - timedelta(seconds=config.storage.idempotency_retention_seconds), ~active_agent
        ).order_by(request.c.created_at, request.c.id).limit(limit)).mappings().all()
    event, notification = tables["event"], tables["notification"]
    referenced = select(notification.c.id).where(notification.c.event_id == event.c.id).exists()
    # Status transitions and provider/source observations are compact audit evidence.
    # Only reconstructible dashboard invalidations are automatically reclaimed.
    events = conn.execute(select(event.c.id).where(event.c.kind == "record_changed", event.c.source == "control_plane",
        event.c.created_at < now - timedelta(seconds=config.storage.event_replay_retention_seconds), ~referenced
        ).order_by(event.c.sequence).limit(limit)).scalars().all()
    return {"api_requests": [{"id": str(row["id"]), "revision": row["revision"]} for row in requests],
            "events": [str(value) for value in events], "batch_limit": limit,
            "protected": ["unsettled API requests", "live execution receipts", "referenced notification events", "semantic events", "provider resume state"]}


def apply_database_retention(conn, config, preview: dict) -> dict:
    """Revalidate the reviewed batch in the same transaction as deletion."""
    from uuid import UUID
    transaction_lock(conn)
    current = database_retention_preview(conn, config, limit=min(int(preview.get("batch_limit", 500)), 1000))
    expected = {(item["id"], item["revision"]) for item in preview.get("api_requests", [])}
    requests = [UUID(item["id"]) for item in current["api_requests"] if (item["id"], item["revision"]) in expected]
    events = [UUID(value) for value in current["events"] if value in set(preview.get("events", []))]
    if requests:
        conn.execute(delete(tables["api_request"]).where(tables["api_request"].c.id.in_(requests)))
    if events:
        conn.execute(delete(tables["event"]).where(tables["event"].c.id.in_(events)))
    return {"api_requests_removed": len(requests), "events_removed": len(events)}


def postgres_environment(url: str) -> dict:
    parsed = make_url(url)
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    for key, value in (("PGHOST", parsed.host), ("PGPORT", parsed.port), ("PGDATABASE", parsed.database),
                       ("PGUSER", parsed.username), ("PGPASSWORD", parsed.password)):
        if value is not None:
            env[key] = str(value)
    for option in ("sslmode", "sslrootcert", "sslcert", "sslkey"):
        if option in parsed.query:
            env["PG" + option.upper()] = parsed.query[option]
    return env


def backup(database, config, destination: Path, *, pg_dump: str = "pg_dump") -> dict:
    """Pin a database snapshot and all its live shared blobs in a new backup directory."""
    if not destination.is_absolute() or ".." in destination.parts:
        raise ValueError("backup destination must be an explicit absolute directory")
    if destination.exists():
        raise ValueError("backup destination must not exist")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".horizon-backup-", dir=destination.parent))
    try:
        with database.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                # pg_dump imports this snapshot; keep it alive during disk/network I/O.
                conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
                snapshot_id = conn.execute(text("SELECT pg_export_snapshot()" )).scalar_one()
                artifact, location = tables["artifact"], tables["artifact_location"]
                rows = list(conn.execute(select(artifact).join(location).where(artifact.c.kind == "blob",
                    location.c.host_id.is_(None), location.c.removed_at.is_(None)).distinct()).mappings())
                manifest = []
                blobs = stage / "blobs"
                blobs.mkdir(mode=0o700)
                from ..persistence.artifacts import ArtifactStore
                store = ArtifactStore(config.artifact_root)
                for row in rows:
                    content = row["content"]
                    data = store.read(content["sha256"], content["size_bytes"])
                    target = blobs / content["sha256"]
                    if not target.exists():
                        with target.open("xb") as handle:
                            handle.write(data)
                            handle.flush()
                            os.fsync(handle.fileno())
                    manifest.append({"artifact_id": str(row["id"]), **content})
                result = subprocess.run([pg_dump, "--format=custom", "--no-owner", "--no-acl", f"--schema={database.schema}",
                    f"--snapshot={snapshot_id}", f"--file={stage / 'database.dump'}"],
                    env=postgres_environment(config.database_url.get_secret_value()), capture_output=True, timeout=3600)
                if result.returncode:
                    raise DomainError("backup_failed", "PostgreSQL backup failed; retained database is unchanged", 503)
        with (stage / "database.dump").open("rb") as dump:
            database_digest = hashlib.file_digest(dump, "sha256").hexdigest()
            os.fsync(dump.fileno())
        manifest_record = {"schema_version": 1, "database_schema": database.schema,
                           "created_at": datetime.now(timezone.utc).isoformat(), "artifacts": manifest,
                           "database_sha256": database_digest, "configuration": config.redacted(),
                           "excluded": ["operator secrets", "worker journals", "provider state", "unpublished workspace files"]}
        with (stage / "manifest.json").open("xb") as output:
            output.write(canonical(manifest_record))
            output.flush()
            os.fsync(output.fileno())
        for directory in (blobs, stage):
            fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        stage.rename(destination)
        fd = os.open(destination.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"path": str(destination), "artifacts": len(manifest)}
    except BaseException:
        # Partial output remains identifiable for diagnosis; no source data is removed.
        raise


def verify_backup(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_bytes())
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(manifest.get("artifacts"), list):
        raise ValueError("unsupported backup manifest")
    with (path / "database.dump").open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != manifest["database_sha256"]:
            raise ValueError("database backup checksum does not match")
    for blob in manifest["artifacts"]:
        digest = blob["sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid artifact identity in backup")
        file = path / "blobs" / digest
        with file.open("rb") as handle:
            if file.stat().st_size != blob["size_bytes"] or hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                raise ValueError("artifact backup checksum does not match")
    return manifest
