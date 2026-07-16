"""Git model: the single out-of-tree workspace ledger."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from archon_horizon.vcs import git_available
from archon_horizon.vcs.git import WorkspaceGit

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


def test_files_in_commit_lists_touched_paths(tmp_path: Path) -> None:
    # The system log's "what was committed" line reads the commit's file list —
    # including the very first commit (no parent), which needs --root.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "index.html").write_text("<html>", "utf-8")
    sha = git.commit("initial", paths=["config.yaml", "dashboard"])
    assert sha
    files = git.files_in_commit(sha)
    assert "config.yaml" in files
    assert "dashboard/index.html" in files


def test_workspace_git_is_out_of_tree(tmp_path: Path) -> None:
    # The workspace repo must never create a root .git that would collide with
    # the user's own repository; it lives under .archon-horizon/vcs/.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    assert not (tmp_path / ".git").exists()
    assert git.git_dir == tmp_path / ".archon-horizon" / "vcs" / "workspace.git"
    assert git.git_dir.exists()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    assert git.commit("initial")  # commits without a root .git


def test_workspace_git_force_adds_horizon_ledger_paths(tmp_path: Path) -> None:
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / ".gitignore").write_text(".archon-horizon/\n", "utf-8")
    state_file = tmp_path / ".archon-horizon" / "events.jsonl"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}\n", "utf-8")

    sha = git.commit("ledger", paths=[".archon-horizon/events.jsonl"])

    assert sha
    # The workspace git is out-of-tree, so query it via its --git-dir/--work-tree.
    tracked = subprocess.run(
        ["git", "--git-dir", str(git.git_dir), "--work-tree", str(tmp_path),
         "ls-files", ".archon-horizon/events.jsonl"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ".archon-horizon/events.jsonl" in tracked


def test_project_files_tracked_through_workspace_ledger(tmp_path: Path) -> None:
    # There is no per-project repository: a project's history is the single
    # workspace ledger filtered by pathspec.
    _configure_identity(tmp_path)
    proj_dir = tmp_path / "projects" / "p"
    proj_dir.mkdir(parents=True)
    (proj_dir / "Foo.lean").write_text("def foo := 1\n", "utf-8")

    git = WorkspaceGit(tmp_path)
    git.init()
    sha = git.commit("add foo", paths=["projects/p"])
    assert sha
    assert not (proj_dir / ".git").exists()  # no nested repo in the tree
    assert "projects/p/Foo.lean" in git.files_in_commit(sha)
    assert [row["sha"] for row in git.log(paths=["projects/p"])] == [sha]
