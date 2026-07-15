"""Version control: the workspace's single out-of-tree ledger repository."""

from __future__ import annotations

from .git import (
    GitError,
    ProjectGit,
    WorkspaceGit,
    collect_revisions,
    git_available,
    project_git_for,
)
from .integration import (
    CommitOutcome,
    SessionIntegration,
    author_for,
    integrate_workspace_baseline,
    integrate_workspace_run,
    integrate_workspace_session,
)

__all__ = [
    "CommitOutcome",
    "GitError",
    "ProjectGit",
    "SessionIntegration",
    "WorkspaceGit",
    "author_for",
    "collect_revisions",
    "git_available",
    "integrate_workspace_baseline",
    "integrate_workspace_run",
    "integrate_workspace_session",
    "project_git_for",
]
