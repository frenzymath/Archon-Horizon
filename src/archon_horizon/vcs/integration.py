"""Session-level VCS integration policy.

The workspace repository is the one integration ledger: one commit per completed
*session*, recording shared Horizon state plus the session's scoped project
worktrees as they stand at that boundary. Committing per session (rather than
only at run end) means a run that crashes or is interrupted still leaves every
finished session durably recorded. There is no separate per-project repository —
a project's history is this repo filtered by pathspec.

Structured provenance rides each commit as git trailers (``Archon-Run``,
``Archon-Round``, ``Archon-Role``, ``Archon-Session``, ``Archon-Task``,
``Archon-Projects``), so the dashboard and agents can map a session to its
commit and read what it touched without parsing prose.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from archon_horizon.core.workspace import Workspace

from .git import GitError, WorkspaceGit, git_available, neutralize_nested_git


# Commit author per acting agent, so `git log --author` / blame separate the two.
# The committer identity stays the system one (see git._git_env).
_ROLE_AUTHORS: dict[str, tuple[str, str]] = {
    "ground": ("Archon Horizon (Ground)", "ground@archon-horizon.local"),
    "horizon": ("Archon Horizon (Horizon)", "horizon@archon-horizon.local"),
}


def author_for(role: str | None) -> tuple[str, str] | None:
    return _ROLE_AUTHORS.get((role or "").strip().lower())

# Serializes workspace commits across parallel sessions. The in-process lock
# keeps threads ordered; the filesystem queue below keeps separate Horizon
# processes from racing on Git's index.
_WORKSPACE_COMMIT_LOCK = threading.Lock()
_COMMIT_QUEUE_POLL_S = 0.25


def _process_alive(pid: int, host: str) -> bool:
    if host != socket.gethostname():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None


@contextmanager
def _workspace_commit_queue(workspace: Workspace):
    """Cross-process queue for workspace integration commits.

    Git has its own ``index.lock``, but failing on that lock makes concurrent
    sessions lose commits. This lock waits instead. A dead holder is reclaimed
    using the same pid/host check as the run lock.
    """
    path = workspace.state_path / "locks" / "commit.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "workspace": workspace.name,
        "created_at": time.time(),
    }
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = _read_lock(path)
            if holder is not None and _process_alive(
                int(holder.get("pid") or 0), str(holder.get("host") or "")
            ):
                time.sleep(_COMMIT_QUEUE_POLL_S)
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        break
    try:
        yield
    finally:
        holder = _read_lock(path)
        if holder is not None and int(holder.get("pid") or 0) == os.getpid():
            try:
                path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    attempted: bool = False
    sha: str | None = None
    error: str | None = None
    changed: bool = False
    files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionIntegration:
    run_id: str
    session: str
    role: str
    project: str | None = None
    task_id: str | None = None
    projects: tuple[str, ...] = ()
    workspace_commit: str | None = None
    workspace_commit_error: str | None = None
    workspace_files: tuple[str, ...] = ()


def _workspace_commit_paths(workspace: Workspace, projects: tuple[str, ...] = ()) -> list[str]:
    """Paths staged by the workspace integration commit.

    Stage shared Horizon state plus the scoped project worktrees. Do not stage
    ``.archon-horizon/vcs`` (the git dir) or ``.archon-horizon/locks``
    (ephemeral leases).
    """
    candidates = [
        workspace.root / "config.yaml",
        workspace.state_path / "version",
        workspace.state_path / "events.jsonl",
        workspace.state_path / "inbox",
        workspace.state_path / "tasks",
        workspace.state_path / "runs",
        workspace.state_path / "reports",
        workspace.state_path / "artifacts",
        workspace.state_path / "blueprints",
        workspace.state_path / "memory.md",
        workspace.state_path / "roadmap",
    ]
    for name in projects:
        if name in workspace.projects:
            candidates.append(workspace.project_path(name))
    paths = [path.relative_to(workspace.root).as_posix() for path in candidates if path.exists()]
    return sorted(dict.fromkeys(paths))


def _commit_trailers(
    *,
    run_id: str,
    session: str,
    role: str,
    round_index: int | None,
    task_id: str | None,
    projects: tuple[str, ...],
) -> dict[str, str]:
    """Machine-queryable provenance appended to the commit message."""
    return {
        "Archon-Run": run_id,
        "Archon-Round": "" if round_index is None else str(round_index),
        "Archon-Role": role,
        "Archon-Session": session,
        "Archon-Task": task_id or "",
        "Archon-Projects": ",".join(projects),
    }


def integrate_workspace_run(
    workspace: Workspace,
    *,
    run_id: str,
    projects: tuple[str, ...] = (),
    message: str | None = None,
    author: tuple[str, str] | None = None,
    trailers: dict[str, str] | None = None,
) -> CommitOutcome:
    """Commit root workspace state (shared state + scoped project worktrees)."""
    if not git_available():
        return CommitOutcome(attempted=False, error="git not available")
    try:
        # Neutralize any in-tree .git in the scoped projects, so the ledger stores
        # their files rather than a submodule gitlink (e.g. a project cloned with
        # its own git). Runs even when no Horizon session touched the project.
        for name in projects:
            if name in workspace.projects:
                neutralize_nested_git(workspace.project_path(name))
        with _WORKSPACE_COMMIT_LOCK:
            with _workspace_commit_queue(workspace):
                git = WorkspaceGit(workspace.root)
                git.init()
                # Drop any stale submodule gitlink left from before a project's nested
                # .git was neutralized, so the commit below re-tracks its files.
                for name in projects:
                    if name in workspace.projects:
                        git.unstage_gitlink(workspace.project_path(name).relative_to(workspace.root).as_posix())
                sha = git.commit(
                    message or f"workspace: integrate run {run_id}",
                    paths=_workspace_commit_paths(workspace, projects),
                    author=author,
                    trailers=trailers,
                )
                files = git.files_in_commit(sha) if sha else ()
        return CommitOutcome(attempted=True, sha=sha, changed=sha is not None, files=files)
    except GitError as exc:
        return CommitOutcome(attempted=True, error=str(exc))


def project_checkpoint(
    workspace: Workspace,
    project: str,
    *,
    message: str | None = None,
    author: tuple[str, str] | None = None,
) -> CommitOutcome:
    """Compatibility wrapper for callers that still checkpoint one project.

    The current VCS model has a single workspace ledger, so a "project
    checkpoint" is just a workspace commit scoped to that project's worktree.
    """
    return integrate_workspace_run(
        workspace,
        run_id="checkpoint",
        projects=(project,),
        message=message or f"project[{project}]: checkpoint",
        author=author,
    )


def integrate_workspace_session(
    workspace: Workspace,
    *,
    run_id: str,
    session: str,
    role: str,
    round_index: int | None = None,
    project: str | None = None,
    task_id: str | None = None,
    projects: tuple[str, ...] = (),
    commit_workspace: bool = True,
) -> SessionIntegration:
    """Integrate one completed session into the workspace ledger.

    Commits the shared Horizon state plus the session's scoped project worktrees
    (``projects``) so a run that never reaches its end still leaves each finished
    session durably recorded, with provenance in git trailers. Pass
    ``commit_workspace=False`` to skip the commit (e.g. a dry run); the returned
    outcome then carries no workspace sha.
    """
    scoped = tuple(dict.fromkeys([*projects, *( (project,) if project else () )]))

    workspace_sha = None
    workspace_error = None
    workspace_files: tuple[str, ...] = ()
    if commit_workspace:
        round_tag = f" r{round_index}" if round_index is not None else ""
        task_tag = f" {task_id}" if task_id else ""
        subject = f"workspace[{run_id}{round_tag}] {role}{task_tag}: integrate {session}"
        workspace_commit = integrate_workspace_run(
            workspace,
            run_id=run_id,
            projects=scoped,
            message=subject,
            author=author_for(role),
            trailers=_commit_trailers(
                run_id=run_id,
                session=session,
                role=role,
                round_index=round_index,
                task_id=task_id,
                projects=scoped,
            ),
        )
        workspace_sha = workspace_commit.sha
        workspace_error = workspace_commit.error
        workspace_files = workspace_commit.files

    return SessionIntegration(
        run_id=run_id,
        session=session,
        role=role,
        project=project,
        task_id=task_id,
        projects=scoped,
        workspace_commit=workspace_sha,
        workspace_commit_error=workspace_error,
        workspace_files=workspace_files,
    )
