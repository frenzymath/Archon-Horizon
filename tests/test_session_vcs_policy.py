"""Session VCS policy: project checkpoints, workspace integration, persistent locks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archon_horizon.config import operations
from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.core.tasks import WriteSet
from archon_horizon.core.workspace import Project, ProjectVcs, Workspace
from archon_horizon.orchestration.locks import FilesystemLockManager
from archon_horizon.vcs.git import git_available
from archon_horizon.vcs.integration import integrate_workspace_session, project_checkpoint


pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _identity() -> None:
    os.environ.setdefault("GIT_AUTHOR_NAME", "test")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "test")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


def test_add_project_enables_and_initializes_project_vcs(tmp_path: Path) -> None:
    _identity()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\nprojects: {}\n", "utf-8")

    operations.add_project(tmp_path, "p", "projects/p")

    cfg = load_config(tmp_path)
    workspace = build_workspace(cfg, tmp_path)
    assert workspace.project("p").vcs.enabled
    assert workspace.project("p").vcs.git_dir == Path(".archon-horizon/vcs/p.git")
    assert (tmp_path / ".archon-horizon" / "vcs" / "p.git").exists()


def test_project_checkpoint_and_workspace_session_integration(tmp_path: Path) -> None:
    _identity()
    project_dir = tmp_path / "projects" / "p"
    project_dir.mkdir(parents=True)
    workspace = Workspace(
        name="ws",
        root=tmp_path,
        projects={
            "p": Project(
                name="p",
                path=Path("projects/p"),
                vcs=ProjectVcs(enabled=True, git_dir=Path(".archon-horizon/vcs/p.git")),
            )
        },
    )
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    (project_dir / "Foo.lean").write_text("def foo := 1\n", "utf-8")

    checkpoint = project_checkpoint(workspace, "p", message="checkpoint")
    assert checkpoint.changed and checkpoint.sha

    integration = integrate_workspace_session(
        workspace,
        run_id="0001",
        session="0001-horizon-T-1",
        role="horizon",
        project="p",
        task_id="T-1",
        project_commits={"p": checkpoint.sha},
    )

    assert integration.manifest_ref == ".archon-horizon/manifests/sessions/0001/0001-horizon-T-1.json"
    assert (tmp_path / integration.manifest_ref).exists()
    assert integration.workspace_commit or integration.workspace_commit_error is None
    assert not (tmp_path / "projects" / "p" / ".git").exists()


def test_filesystem_lock_manager_blocks_overlapping_files(tmp_path: Path) -> None:
    locks = FilesystemLockManager(tmp_path / ".archon-horizon" / "locks")

    assert locks.acquire("run-a", WriteSet(files=("projects/p/Foo.lean",)))
    assert not locks.acquire("run-b", WriteSet(files=("projects/p/Foo.lean",)))
    assert locks.acquire("run-c", WriteSet(files=("projects/q/Bar.lean",)))
    locks.release("run-a")
    assert locks.acquire("run-b", WriteSet(files=("projects/p/Foo.lean",)))

