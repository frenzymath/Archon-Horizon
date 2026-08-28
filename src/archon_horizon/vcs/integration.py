"""Session-level VCS integration policy.

The workspace repository is the one **agent source ledger**: one commit per
completed *session*, recording ``config.yaml`` plus the session's scoped project
worktrees (Lean, blueprints, …) as they stand at that boundary. Committing per
session (rather than only at run end) means a run that crashes or is interrupted
still leaves every finished session's *math* durably recorded. Horizon state
(``.archon-horizon/``) is not part of this journal — it lives on disk for the
dashboard; humans publish snapshots via root ``.git`` or static export. There is
no separate per-project repository — a project's history is this repo filtered
by pathspec.

Structured provenance rides each commit as git trailers (``Archon-Run``,
``Archon-Round``, ``Archon-Role``, ``Archon-Session``, ``Archon-Task``,
``Archon-Projects``), so the dashboard and agents can map a session to its
commit and read what it touched without parsing prose.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from archon_horizon.core.workspace import Workspace

from .git import GitError, WorkspaceGit, git_available, neutralize_nested_git


# Commit author per acting role, so `git log --author` / blame separate them.
# The committer identity stays the system one (see git._git_env).
_ROLE_AUTHORS: dict[str, tuple[str, str]] = {
    "horizon": ("Archon Horizon (Horizon)", "horizon@archon-horizon.local"),
    "system": ("Archon Horizon (System)", "system@archon-horizon.local"),
}


def author_for(role: str | None) -> tuple[str, str] | None:
    return _ROLE_AUTHORS.get((role or "").strip().lower())

# Orders integration commits when the dashboard server thread and the run loop
# commit from the same process.
_WORKSPACE_COMMIT_LOCK = threading.Lock()


@contextmanager
def _cross_process_commit_lock(workspace: Workspace, timeout: float = 120.0):
    """Serialize boundary commits across concurrent ``horizon run`` processes.

    A workspace deliberately running several projects in parallel (one run per
    project, possibly under different accounts) shares ONE ledger; without this,
    two runs starting in the same second race the baseline commit and one fails
    with ``cannot lock ref 'HEAD': is at X but expected Y``. An OS ``flock`` is
    used because it dies with the holder — no stale-lock reclaim needed. On
    platforms without ``fcntl`` (Windows) this degrades to no cross-process
    lock; the retry in :meth:`WorkspaceGit.commit` still resolves races there.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    lock_dir = workspace.state_path / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / "workspace-commit.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise GitError(
                        f"timed out after {timeout:.0f}s waiting for the workspace "
                        "commit lock (another run is committing to the ledger)"
                    )
                time.sleep(0.2)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


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

    The agent ledger records **sources that matter for proofs**: ``config.yaml``
    and the session's scoped project worktrees (Lean, blueprints, …). Horizon
    control-plane state (``.archon-horizon/`` — inbox, tasks, runs, roadmap,
    blueprint JSON cache) stays on disk for the live dashboard and is never
    staged here. Generated ``hgraph/`` trees are excluded by ledger policy.
    Users publish state via their own root ``.git`` or ``horizon dashboard --static``.
    """
    candidates = [workspace.root / "config.yaml"]
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
    commit_kind: str = "",
) -> dict[str, str]:
    """Machine-queryable provenance appended to the commit message."""
    return {
        "Archon-Run": run_id,
        "Archon-Round": "" if round_index is None else str(round_index),
        "Archon-Role": role,
        "Archon-Session": session,
        "Archon-Task": task_id or "",
        "Archon-Projects": ",".join(projects),
        "Archon-Commit": commit_kind,
    }


def integrate_workspace_run(
    workspace: Workspace,
    *,
    run_id: str,
    projects: tuple[str, ...] = (),
    message: str | None = None,
    author: tuple[str, str] | None = None,
    trailers: dict[str, str] | None = None,
    allow_empty: bool = False,
) -> CommitOutcome:
    """Commit agent-relevant sources (config + scoped project worktrees)."""
    if not git_available():
        return CommitOutcome(attempted=False, error="git not available")
    try:
        # Neutralize any in-tree .git in the scoped projects, so the ledger stores
        # their files rather than a submodule gitlink (e.g. a project cloned with
        # its own git). Runs even when no Horizon session touched the project.
        for name in projects:
            if name in workspace.projects:
                neutralize_nested_git(workspace.project_path(name))
        with _WORKSPACE_COMMIT_LOCK, _cross_process_commit_lock(workspace):
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
                allow_empty=allow_empty,
            )
            files = git.files_in_commit(sha) if sha else ()
        return CommitOutcome(attempted=True, sha=sha, changed=sha is not None, files=files)
    except GitError as exc:
        return CommitOutcome(attempted=True, error=str(exc))


def integrate_workspace_baseline(
    workspace: Workspace,
    *,
    run_id: str,
    projects: tuple[str, ...] = (),
) -> CommitOutcome:
    """Append a run-start baseline marker to the workspace ledger.

    The baseline is allowed to be empty: it is an intuitive anchor saying
    "compare the first agentic commit in this run against here", even when the
    worktree already matched the previous ledger head.
    """
    return integrate_workspace_run(
        workspace,
        run_id=run_id,
        projects=projects,
        message=f"workspace[{run_id}] system: baseline",
        author=author_for("system"),
        trailers=_commit_trailers(
            run_id=run_id,
            session="run-baseline",
            role="system",
            round_index=None,
            task_id=None,
            projects=projects,
            commit_kind="baseline",
        ),
        allow_empty=True,
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

    Commits ``config.yaml`` plus the session's scoped project worktrees
    (``projects``) so a run that never reaches its end still leaves each finished
    session's sources durably recorded, with provenance in git trailers. Pass
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
                commit_kind="integration",
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
