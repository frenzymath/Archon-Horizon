"""Verify existing local Git checkouts without modifying their files or branches."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess

from sqlalchemy import select

from ..auth import authenticate, require_admin
from ..projects.catalog import CatalogUpdate, update_catalog
from ..errors import DomainError
from ..persistence.records import get, transaction_lock
from ..persistence.schema import tables
from ..worker_config import WorkerConfig, private_text


def _host(conn, worker):
    actor = authenticate(conn, private_text(worker.token_file))
    if actor.kind != "host" or actor.owner.get("host_id") != str(worker.host_id):
        raise DomainError("host_mismatch", "Worker configuration and credential must identify the same enrolled host", 403)
    return get(conn, "host", worker.host_id)


def inspect_checkout(row, worker: WorkerConfig):
    path = Path(row["path"])
    roots = [root.resolve(strict=True) for root in worker.workspace_roots]
    resolved = path.resolve(strict=True)
    if resolved != path or not any(root in resolved.parents for root in roots):
        raise ValueError("workspace must be a real directory strictly inside an enabled local root")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", row["base_commit_oid"]):
        raise ValueError("workspace base must pin a complete Git commit identity")
    env = {"PATH": os.defpath, "HOME": "/nonexistent", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    base = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false"]
    def git(*args, allowed=(0,)):
        result = subprocess.run([*base, *args], cwd=resolved, env=env, capture_output=True, timeout=30)
        if result.returncode not in allowed:
            raise ValueError(f"read-only Git {args[0]} verification failed")
        if len(result.stdout) > 1024**2:
            raise ValueError("workspace verification output exceeds one MiB")
        return result.stdout.decode("utf-8", errors="replace").strip()
    filters = git("config", "--includes", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process|required)$", allowed=(0, 1))
    keys = [key for key in filters.split("\0") if key]
    if len(keys) > 128:
        raise ValueError("too many repository filter configurations")
    for key in keys:
        base.extend(("-c", key + ("=false" if key.endswith(".required") else "=")))
    if Path(git("rev-parse", "--show-toplevel")).resolve() != resolved:
        raise ValueError("registered workspace must be the Git worktree root")
    branch = git("symbolic-ref", "--short", "HEAD")
    if branch != row["branch_name"]:
        raise ValueError("checkout branch does not match its registered workspace")
    head = git("rev-parse", "--verify", "HEAD^{commit}")
    git("merge-base", "--is-ancestor", row["base_commit_oid"], head)
    dirty = bool(git("status", "--porcelain=v1", "--untracked-files=normal", "--ignore-submodules=all"))
    if dirty:
        raise ValueError("initial workspace contains uncommitted or untracked files; preserve and reconcile them before admission")
    return {"head_commit_oid": head, "branch_name": branch}


def verify(database, actor, worker: WorkerConfig, *, apply=False):
    with database.transaction() as conn:
        require_admin(conn, actor)
        host = _host(conn, worker)
        registered_root = Path(host["workspace_root"]).resolve(strict=True)
        if registered_root not in [root.resolve(strict=True) for root in worker.workspace_roots]:
            raise DomainError("root_mismatch", "The enrolled host root must appear in this local worker configuration")
        workspace = tables["workspace"]
        rows = [dict(row) for row in conn.execute(select(workspace).where(workspace.c.host_id == worker.host_id,
            workspace.c.status.in_(("preparing", "unavailable"))).order_by(workspace.c.path).limit(101)).mappings()]
    has_more = len(rows) > 100
    rows = rows[:100]
    results = []
    for row in rows:
        result = {"id": str(row["id"]), "revision": row["revision"], "path": row["path"]}
        try:
            result.update(inspect_checkout(row, worker), ready=True)
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result.update(ready=False, reason=str(error))
        results.append(result)
    if apply and any(not row["ready"] for row in results):
        raise DomainError("workspace_verification_failed", "No workspaces were changed; inspect the read-only verification results first")
    if apply:
        with database.transaction() as conn:
            transaction_lock(conn)
            require_admin(conn, actor)
            _host(conn, worker)
            for row in results:
                current = get(conn, "workspace", row["id"], lock=True)
                if current["host_id"] != worker.host_id or current["status"] not in ("preparing", "unavailable"):
                    raise DomainError("workspace_changed", "Workspace eligibility changed; repeat verification")
                update_catalog(conn, actor, "workspace", current["id"], CatalogUpdate(expected_revision=row["revision"],
                    changes={"status": "ready", "head_commit_oid": row["head_commit_oid"]}))
    return {"host_id": str(worker.host_id), "applied": apply, "workspaces": results, "has_more": has_more}
