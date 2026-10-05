"""Read-only diagnostics for explicitly selected server and worker installations."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import stat
import subprocess


def database_checks(conn):
    from sqlalchemy import func, select
    from .schema import tables
    now = datetime.now(timezone.utc)
    execution = tables["execution"]
    live = execution.c.status.in_(("starting", "running", "stopping"))
    expired = live & (execution.c.lease_expires_at < now)
    unconfirmed = (~live) & execution.c.stop_confirmed_at.is_(None)
    row = conn.execute(select(func.count().filter(live).label("active"),
                              func.count().filter(expired).label("expired"),
                              func.count().filter(unconfirmed).label("unconfirmed"))).mappings().one()
    checks = [{"name": "worker_leases", "status": "attention" if row["expired"] or row["unconfirmed"] else "ready",
               "active": row["active"], "expired": row["expired"], "unconfirmed_stops": row["unconfirmed"]}]
    for name, table_name, settled in (("publications", "publication", ("verified", "cancelled")),
                                       ("outbox", "outbox_operation", ("completed", "cancelled"))):
        table = tables[table_name]
        rows = conn.execute(select(table.c.status, func.count().label("count"),
                                   func.min(table.c.created_at).label("oldest_at"))
                            .where(table.c.status.not_in(settled)).group_by(table.c.status)).mappings().all()
        counts = {row["status"]: row["count"] for row in rows}
        oldest = min((row["oldest_at"] for row in rows), default=None)
        checks.append({"name": name, "status": "attention" if counts.get("failed") or counts.get("uncertain") else "ready",
                       "counts": counts, "oldest_pending_at": oldest.isoformat() if oldest else None,
                       "oldest_pending_age_seconds": max(0, int((now - oldest).total_seconds())) if oldest else None})
    return checks


def worker_checks(path: Path):
    from .worker_config import WorkerConfig, private_text
    try:
        worker = WorkerConfig.model_validate_json(path.read_bytes())
    except (ValueError, OSError) as error:
        return [{"name": "worker_config", "status": "unavailable", "detail": type(error).__name__}]
    checks = [{"name": "worker_config", "status": "ready", "host_id": str(worker.host_id)}]
    try:
        private_text(worker.token_file)
        checks.append({"name": "worker_host_credential", "status": "ready", "validation": "local_file_only"})
    except (ValueError, OSError) as error:
        checks.append({"name": "worker_host_credential", "status": "unavailable", "detail": type(error).__name__})
    checks.append({"name": "worker_git", "status": "ready" if shutil.which("git") else "missing"})
    for directory in [worker.journal_root, *worker.workspace_roots]:
        checks.append({"name": "worker_directory", "path": str(directory),
                       "status": "ready" if directory.is_dir() and not directory.is_symlink() else "missing"})
    for harness in worker.harnesses:
        identity = str(harness.id)
        binary = shutil.which(harness.executable, path=harness.environment.get("PATH", os.defpath))
        container = harness.sandbox.mode == "rootless_container"
        if container:
            podman = shutil.which("podman")
            available = False
            if podman:
                try:
                    available = subprocess.run([podman, "image", "exists", harness.sandbox.image_digest],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=5, check=False).returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    pass
            checks.append({"name": "worker_sandbox_image", "harness_id": identity,
                           "status": "ready" if available else "missing", "provider_binary": "not_probed_inside_image"})
        else:
            checks.append({"name": "worker_provider_binary", "harness_id": identity,
                           "status": "ready" if binary and os.access(binary, os.X_OK) else "missing"})
        auth = harness.provider_home / (".codex/auth.json" if harness.adapter == "codex_exec" else ".claude/.credentials.json")
        try:
            info = auth.lstat()
            present = stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077 and info.st_uid == os.getuid() and info.st_size > 0
        except OSError:
            present = False
        checks.append({"name": "worker_provider_auth", "harness_id": identity,
                       "status": "ready" if present else "attention", "validation": "private_file_presence_only",
                       "online_authentication": "not_probed"})
    return checks
