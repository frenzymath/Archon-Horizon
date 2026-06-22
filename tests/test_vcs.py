"""Git model: workspace manifest repo and out-of-tree project VCS."""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_horizon.core.workspace import Project, ProjectVcs, Workspace
from archon_horizon.vcs import collect_revisions, git_available, project_git_for
from archon_horizon.vcs.git import ProjectGit, WorkspaceGit

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _configure_identity(env_root: Path) -> None:
    # Commits need an author; set it locally inside each repo via env.
    import os

    os.environ.setdefault("GIT_AUTHOR_NAME", "test")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "test")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


def test_workspace_git_commits(tmp_path: Path) -> None:
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    assert git.is_repo()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    sha = git.commit("initial")
    assert sha and len(sha) >= 7
    assert git.commit("no changes") is None  # nothing to commit


def test_project_git_outside_tree_and_manifest(tmp_path: Path) -> None:
    _configure_identity(tmp_path)
    proj_dir = tmp_path / "projects" / "p"
    proj_dir.mkdir(parents=True)
    git_dir = tmp_path / ".archon-horizon" / "vcs" / "p.git"

    pg = ProjectGit(git_dir=git_dir, work_tree=proj_dir)
    pg.init()
    assert git_dir.exists()
    assert not (proj_dir / ".git").exists()  # no nested repo in the tree
    (proj_dir / "Foo.lean").write_text("def foo := 1\n", "utf-8")
    sha = pg.commit("add foo")
    assert sha

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
    assert project_git_for(workspace, "p") is not None
    assert collect_revisions(workspace)["p"] == sha
