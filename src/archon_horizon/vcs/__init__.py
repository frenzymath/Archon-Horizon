"""Version control: the workspace's single out-of-tree ledger repository."""

from __future__ import annotations

from .git import (
    GitError,
    WorkspaceGit,
    git_available,
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
    "SessionIntegration",
    "WorkspaceGit",
    "author_for",
    "git_available",
    "integrate_workspace_baseline",
    "integrate_workspace_run",
    "integrate_workspace_session",
]
