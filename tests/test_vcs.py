"""Git model: workspace manifest repo and out-of-tree project VCS."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from archon_horizon.core.workspace import Project, ProjectVcs, Workspace
from archon_horizon.vcs import collect_revisions, git_available, project_git_for
from archon_horizon.vcs.git import ProjectGit, WorkspaceGit
from archon_horizon.vcs.integration import _COMMIT_QUEUE_STALE_S, _workspace_commit_queue

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


def test_workspace_commit_queue_waits_for_current_holder(tmp_path: Path) -> None:
    workspace = Workspace(name="ws", root=tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with _workspace_commit_queue(workspace):
            first_entered.set()
            assert release_first.wait(timeout=2.0)

    def second() -> None:
        assert first_entered.wait(timeout=2.0)
        with _workspace_commit_queue(workspace):
            second_entered.set()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()
    assert first_entered.wait(timeout=2.0)
    time.sleep(0.1)
    assert not second_entered.is_set()
    release_first.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert second_entered.is_set()
    assert not (workspace.state_path / "locks" / "commit.lock").exists()


def test_workspace_commit_queue_reclaims_stale_cross_host_lock(tmp_path: Path) -> None:
    # A lock left by a crashed process on *another* host looks alive
    # (`_process_alive` can't probe cross-host and returns True). Without the
    # staleness backstop this would wait forever; an aged `created_at` must let
    # the next committer steal it and proceed.
    workspace = Workspace(name="ws", root=tmp_path)
    lock = workspace.state_path / "locks" / "commit.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({
        "pid": 999999, "host": "some-other-host", "workspace": "ws",
        "created_at": time.time() - _COMMIT_QUEUE_STALE_S - 60,
    }), "utf-8")

    entered = threading.Event()

    def acquire() -> None:
        with _workspace_commit_queue(workspace):
            entered.set()

    t = threading.Thread(target=acquire)
    t.start()
    assert entered.wait(timeout=2.0), "stale cross-host lock was not reclaimed"
    t.join(timeout=2.0)
    assert not lock.exists()


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
