"""Session-level VCS integration policy.

Project repositories are detailed work journals: checkpoint them at completed
Horizon steps. The workspace repository is the integration ledger: one
manifest commit per completed top-level session, recording the current project
revisions and the workspace artifacts touched by that session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archon_horizon.core.clock import utc_now
from archon_horizon.core.workspace import Workspace
from archon_horizon.store import serde

from .git import GitError, WorkspaceGit, collect_revisions, git_available, project_git_for


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
    manifest_ref: str | None = None


def project_checkpoint(
    workspace: Workspace,
    project: str,
    *,
    message: str,
) -> CommitOutcome:
    """Commit one project's worktree when VCS is enabled and changed."""
    if not git_available():
        return CommitOutcome(attempted=False, error="git not available")
    git = project_git_for(workspace, project)
    if git is None:
        return CommitOutcome(attempted=False)
    try:
        git.init()
        sha = git.commit(message)
        return CommitOutcome(attempted=True, sha=sha, changed=sha is not None)
    except GitError as exc:
        return CommitOutcome(attempted=True, error=str(exc))


def _session_manifest_path(workspace: Workspace, run_id: str, session: str) -> Path:
    return workspace.state_path / "manifests" / "sessions" / run_id / f"{session}.json"


def _current_manifest_path(workspace: Workspace) -> Path:
    return workspace.state_path / "manifests" / "current.json"


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(serde.to_jsonable(payload), indent=2, sort_keys=True), "utf-8")
    return path.as_posix()


def _workspace_commit_paths(workspace: Workspace) -> list[str]:
    """Paths owned by the workspace git ledger.

    Do not stage ``.archon-horizon/vcs`` (project git dirs) or
    ``.archon-horizon/locks`` (ephemeral leases).
    """
    candidates = [
        workspace.root / "config.yaml",
        workspace.state_path / "events.jsonl",
        workspace.state_path / "inboxes",
        workspace.state_path / "tasks",
        workspace.state_path / "proposals",
        workspace.state_path / "runs",
        workspace.state_path / "reports",
        workspace.state_path / "artifacts",
        workspace.state_path / "blueprints",
        workspace.state_path / "manifests",
        workspace.state_path / "memory.md",
        workspace.state_path / "roadmap.yaml",
    ]
    return [path.relative_to(workspace.root).as_posix() for path in candidates if path.exists()]


def integrate_workspace_session(
    workspace: Workspace,
    *,
    run_id: str,
    session: str,
    role: str,
    project: str | None = None,
    task_id: str | None = None,
    project_commits: dict[str, str | None] | None = None,
    project_commit_errors: dict[str, str] | None = None,
) -> SessionIntegration:
    """Write session manifests and commit workspace state once.

    This function owns workspace shared-state commits. It stages only
    ``config.yaml`` and ``.archon-horizon`` so concurrent project worktrees are
    not accidentally swept into the workspace repository.
    """
    revisions = collect_revisions(workspace)
    project_commits = project_commits or {}
    project_commit_errors = project_commit_errors or {}
    payload = {
        "run_id": run_id,
        "session": session,
        "role": role,
        "project": project,
        "task_id": task_id,
        "integrated_at": utc_now().isoformat(),
        "project_revisions": revisions,
        "project_commits": project_commits,
        "project_commit_errors": project_commit_errors,
    }
    manifest = _session_manifest_path(workspace, run_id, session)
    manifest_ref = _write_json(manifest, payload)
    _write_json(_current_manifest_path(workspace), {
        "updated_at": payload["integrated_at"],
        "last_run_id": run_id,
        "last_session": session,
        "project_revisions": revisions,
    })

    workspace_sha = None
    workspace_error = None
    if git_available():
        try:
            git = WorkspaceGit(workspace.root)
            git.init()
            workspace_sha = git.commit(
                f"workspace: integrate session {run_id}/{session}",
                paths=_workspace_commit_paths(workspace),
            )
        except GitError as exc:
            workspace_error = str(exc)
    else:
        workspace_error = "git not available"

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
        manifest_ref=Path(manifest_ref).relative_to(workspace.root).as_posix(),
    )
