"""Version control: the workspace manifest repo and per-project VCS wrappers."""

from __future__ import annotations

from .git import (
    GitError,
    ProjectGit,
    WorkspaceGit,
    collect_revisions,
    git_available,
    project_git_for,
)
from .integration import CommitOutcome, SessionIntegration, integrate_workspace_run, integrate_workspace_session, project_checkpoint

__all__ = [
    "CommitOutcome",
    "GitError",
    "ProjectGit",
    "SessionIntegration",
    "WorkspaceGit",
    "collect_revisions",
    "git_available",
    "integrate_workspace_run",
    "integrate_workspace_session",
    "project_git_for",
    "project_checkpoint",
]
