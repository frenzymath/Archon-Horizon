"""Session-level VCS integration policy.

Project repositories are detailed work journals: checkpoint them at completed
Horizon steps. The workspace repository is the shareable integration ledger: one
commit per completed *session*, recording shared state plus the scoped project
files as they stand at that boundary. Committing per session (rather than only
at run end) means a run that crashes or is interrupted still leaves every
finished session durably recorded.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from archon_horizon.core.workspace import Workspace

from .git import GitError, WorkspaceGit, git_available, neutralize_nested_git, project_git_for


# Commit author per acting agent, so `git log --author` / blame separate the two.
# The committer identity stays the system one (see git._git_env).
_ROLE_AUTHORS: dict[str, tuple[str, str]] = {
    "ground": ("Archon Horizon (Ground)", "ground@archon-horizon.local"),
    "horizon": ("Archon Horizon (Horizon)", "horizon@archon-horizon.local"),
}


def author_for(role: str | None) -> tuple[str, str] | None:
    return _ROLE_AUTHORS.get((role or "").strip().lower())

# Serializes workspace commits across parallel sessions in one process so they
# do not race on the git index. Cross-process runs are guarded by git's own
# ``index.lock``; a losing commit surfaces as ``workspace_commit_error``.
_WORKSPACE_COMMIT_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    attempted: bool = False
    sha: str | None = None
    error: str | None = None
    changed: bool = False


@dataclass(frozen=True, slots=True)
class SessionIntegration:
    run_id: str
    session: str
    role: str
    project: str | None = None
    task_id: str | None = None
    project_commits: dict[str, str | None] = field(default_factory=dict)
    project_commit_errors: dict[str, str] = field(default_factory=dict)
    workspace_commit: str | None = None
    workspace_commit_error: str | None = None


def project_checkpoint(
    workspace: Workspace,
    project: str,
    *,
    message: str,
    author: tuple[str, str] | None = None,
) -> CommitOutcome:
    """Commit one project's worktree when VCS is enabled and changed."""
    if not git_available():
        return CommitOutcome(attempted=False, error="git not available")
    git = project_git_for(workspace, project)
    if git is None:
        return CommitOutcome(attempted=False)
    try:
        git.init()
        sha = git.commit(message, author=author)
        return CommitOutcome(attempted=True, sha=sha, changed=sha is not None)
    except GitError as exc:
        return CommitOutcome(attempted=True, error=str(exc))


def ensure_project_baseline(workspace: Workspace, project: str) -> CommitOutcome:
    """Ensure a registered project's repo has an initial commit, so its tree is
    tracked from the moment it is added rather than sitting on an empty branch
    (the "no commits yet" state seen when a project is never scheduled)."""
    if not git_available():
        return CommitOutcome(attempted=False, error="git not available")
    git = project_git_for(workspace, project)
    if git is None:
        return CommitOutcome(attempted=False)
    try:
        sha = git.ensure_initial_commit()
        return CommitOutcome(attempted=True, sha=sha, changed=sha is not None)
    except GitError as exc:
        return CommitOutcome(attempted=True, error=str(exc))


def _workspace_commit_paths(workspace: Workspace, projects: tuple[str, ...] = ()) -> list[str]:
    """Paths staged by the workspace integration commit.

    Stage shared Horizon state plus the scoped project worktrees. Do not stage
    ``.archon-horizon/vcs`` (project git dirs) or ``.archon-horizon/locks``
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


def integrate_workspace_run(
    workspace: Workspace,
    *,
    run_id: str,
    projects: tuple[str, ...] = (),
    message: str | None = None,
    author: tuple[str, str] | None = None,
) -> CommitOutcome:
    """Commit root workspace state for a completed run or session."""
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
            )
        return CommitOutcome(attempted=True, sha=sha, changed=sha is not None)
    except GitError as exc:
        return CommitOutcome(attempted=True, error=str(exc))


def _format_workspace_message(
    *,
    run_id: str,
    session: str,
    role: str,
    round_index: int | None,
    task_id: str | None,
    project_commits: dict[str, str | None],
) -> str:
    """Structured ledger message: a scannable subject encoding which step this
    was (run / round / role / task), plus a body with the per-project shas."""
    round_tag = f" r{round_index}" if round_index is not None else ""
    task_tag = f" {task_id}" if task_id else ""
    subject = f"workspace[{run_id}{round_tag}] {role}{task_tag}: integrate {session}"
    body = [
        f"Run: {run_id}",
        f"Round: {round_index if round_index is not None else '—'}",
        f"Role: {role}",
        f"Session: {session}",
        f"Task: {task_id or '—'}",
    ]
    committed = {proj: sha for proj, sha in project_commits.items() if sha}
    if committed:
        body.append("Project commits:")
        body.extend(f"  {proj}: {sha[:10]}" for proj, sha in committed.items())
    return subject + "\n\n" + "\n".join(body)


def integrate_workspace_session(
    workspace: Workspace,
    *,
    run_id: str,
    session: str,
    role: str,
    round_index: int | None = None,
    project: str | None = None,
    task_id: str | None = None,
    project_commits: dict[str, str | None] | None = None,
    project_commit_errors: dict[str, str] | None = None,
    commit_workspace: bool = True,
) -> SessionIntegration:
    """Integrate one completed session into the workspace ledger.

    Commits the shared Horizon state plus the session's scoped project worktrees
    so a run that never reaches its end still leaves each finished session
    durably recorded. Pass ``commit_workspace=False`` to skip the commit (e.g. a
    dry run); the returned outcome then carries no workspace sha.
    """
    project_commits = project_commits or {}
    project_commit_errors = project_commit_errors or {}

    workspace_sha = None
    workspace_error = None
    if commit_workspace:
        scoped_project_names = list(project_commits)
        if project:
            scoped_project_names.append(project)
        workspace_commit = integrate_workspace_run(
            workspace,
            run_id=run_id,
            projects=tuple(dict.fromkeys(scoped_project_names)),
            message=_format_workspace_message(
                run_id=run_id,
                session=session,
                role=role,
                round_index=round_index,
                task_id=task_id,
                project_commits=project_commits,
            ),
            author=author_for(role),
        )
        workspace_sha = workspace_commit.sha
        workspace_error = workspace_commit.error

    return SessionIntegration(
        run_id=run_id,
        session=session,
        role=role,
        project=project,
        task_id=task_id,
        project_commits=project_commits,
        project_commit_errors=project_commit_errors,
        workspace_commit=workspace_sha,
        workspace_commit_error=workspace_error,
    )
