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


def test_workspace_git_resolves_relative_root(tmp_path: Path, monkeypatch) -> None:
    _configure_identity(tmp_path)
    workspace = tmp_path / "demo"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)

    git = WorkspaceGit(Path("demo"))
    git.init()
    (workspace / "config.yaml").write_text("x\n", "utf-8")

    assert git.root == workspace
    assert git.git_dir == workspace / ".archon-horizon" / "vcs" / "workspace.git"
    assert git.commit("initial", paths=["config.yaml"])


def test_commits_detailed_by_refs_handles_legacy_untagged_commit(
    tmp_path: Path, monkeypatch
) -> None:
    _configure_identity(tmp_path)
    for key in (
        "ARCHON_HORIZON_RUN",
        "ARCHON_HORIZON_SESSION",
        "ARCHON_HORIZON_AGENT_ROLE",
    ):
        monkeypatch.delenv(key, raising=False)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    sha = git.commit("legacy interactive work", paths=["config.yaml"])
    assert sha

    rows = git.commits_detailed_by_refs([sha[:10]])
    assert len(rows) == 1
    assert rows[0]["sha"] == sha
    assert rows[0]["kind"] == "agent"


def test_workspace_ledger_excludes_raw_transcripts_and_usage(tmp_path: Path) -> None:
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    session = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions" / "s"
    session.mkdir(parents=True)
    (session / "transcript.jsonl").write_text("{\"secret\":\"sk-test\"}\n", "utf-8")
    (session / "usage.json").write_text("{}\n", "utf-8")

    assert git.commit("raw session artifacts", paths=[".archon-horizon/runs"]) is None


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


def test_commit_retries_through_ref_race(tmp_path: Path, monkeypatch) -> None:
    # A writer we didn't see (an agent's plain git, another run's boundary
    # commit) can advance HEAD between commit()'s staleness check and the git
    # commit itself; git rejects with "cannot lock ref 'HEAD'". That must be
    # retried against the new HEAD, not surfaced as a hard failure.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    assert git.commit("initial")

    (tmp_path / "config.yaml").write_text("y\n", "utf-8")
    from archon_horizon.vcs.git import GitError

    real_run = WorkspaceGit._run
    failures = {"left": 2}

    def flaky_run(self, args, **kwargs):
        if args and args[0] == "commit" and failures["left"] > 0:
            failures["left"] -= 1
            raise GitError("fatal: cannot lock ref 'HEAD': is at aaa but expected bbb")
        return real_run(self, args, **kwargs)

    monkeypatch.setattr(WorkspaceGit, "_run", flaky_run)
    sha = git.commit("racy")
    assert sha is not None
    assert failures["left"] == 0


def test_commit_raises_after_exhausting_ref_race_retries(tmp_path: Path, monkeypatch) -> None:
    # Losing every attempt must surface as an error — returning None would
    # report "nothing changed" for a commit that was never made.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    assert git.commit("initial")

    (tmp_path / "config.yaml").write_text("y\n", "utf-8")
    from archon_horizon.vcs.git import GitError

    real_run = WorkspaceGit._run

    def always_locked(self, args, **kwargs):
        if args and args[0] == "commit":
            raise GitError("fatal: cannot lock ref 'HEAD': is at aaa but expected bbb")
        return real_run(self, args, **kwargs)

    monkeypatch.setattr(WorkspaceGit, "_run", always_locked)
    with pytest.raises(GitError):
        git.commit("doomed")


def test_concurrent_baseline_commits_serialize(tmp_path: Path) -> None:
    # Several `horizon run` processes against one shared ledger start within
    # the same second; the cross-process lock + ref-race retry must let every
    # baseline land instead of failing with "cannot lock ref 'HEAD'".
    import threading

    from archon_horizon.core.workspace import Workspace
    from archon_horizon.vcs.integration import integrate_workspace_baseline

    _configure_identity(tmp_path)
    (tmp_path / "config.yaml").write_text("projects: {}\n", "utf-8")
    workspace = Workspace(name="ws", root=tmp_path)

    outcomes = {}

    def baseline(run_id: str) -> None:
        outcomes[run_id] = integrate_workspace_baseline(workspace, run_id=run_id)

    threads = [threading.Thread(target=baseline, args=(f"run-{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for run_id, outcome in outcomes.items():
        assert outcome.error is None, f"{run_id}: {outcome.error}"
        assert outcome.sha, f"{run_id} produced no baseline commit"
    assert len({o.sha for o in outcomes.values()}) == 4  # four distinct commits


def _plain_git(git: WorkspaceGit, *args: str, env: dict | None = None):
    # Run plain git against the ledger the way an agent's `hgit` does —
    # through the repo's hooks, no private-index plumbing from WorkspaceGit.
    import os as _os
    import subprocess as _sp

    e = {**_os.environ, **(env or {})}
    e.setdefault("GIT_AUTHOR_NAME", "agent")
    e.setdefault("GIT_AUTHOR_EMAIL", "agent@example.com")
    e.setdefault("GIT_COMMITTER_NAME", "agent")
    e.setdefault("GIT_COMMITTER_EMAIL", "agent@example.com")
    return _sp.run(
        ["git", "--git-dir", str(git.git_dir), "--work-tree", str(git.root), *args],
        cwd=git.root, capture_output=True, text=True, env=e,
    )


def test_hook_blocks_stale_index_clobber(tmp_path: Path) -> None:
    # I-0304: session B seeds a private index from an old HEAD; a concurrent
    # session A commits a new file; B's commit tree lacks A's file, so plain
    # git would delete it silently. The pre-commit guard must reject B.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    assert git.commit("initial")
    old_head = git.current_sha()

    # Concurrent session A lands a new file after B's read-tree base.
    (tmp_path / "a_new_file.txt").write_text("A's work\n", "utf-8")
    assert git.commit("A: add file")

    # Session B: private index seeded from the OLD head + B's own file.
    b_index = tmp_path / "b-index"
    env = {"GIT_INDEX_FILE": str(b_index)}
    assert _plain_git(git, "read-tree", old_head, env=env).returncode == 0
    (tmp_path / "b_file.txt").write_text("B's work\n", "utf-8")
    assert _plain_git(git, "add", "--", "b_file.txt", env=env).returncode == 0

    blocked = _plain_git(git, "commit", "-m", "B: stale-based commit", env=env)
    assert blocked.returncode != 0
    assert "DELETE" in blocked.stderr
    assert "a_new_file.txt" in blocked.stderr

    # Recovery per the skill: re-seed from the current HEAD, re-add, retry.
    assert _plain_git(git, "read-tree", "HEAD", env=env).returncode == 0
    assert _plain_git(git, "add", "--", "b_file.txt", env=env).returncode == 0
    ok = _plain_git(git, "commit", "-m", "B: rebased commit", env=env)
    assert ok.returncode == 0, ok.stderr
    # Both sessions' files are in HEAD.
    listing = _plain_git(git, "ls-tree", "--name-only", "HEAD")
    assert "a_new_file.txt" in listing.stdout and "b_file.txt" in listing.stdout


def test_hook_allows_intentional_deletion_with_env(tmp_path: Path) -> None:
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    (tmp_path / "doomed.txt").write_text("bye\n", "utf-8")
    assert git.commit("initial")

    idx = tmp_path / "del-index"
    env = {"GIT_INDEX_FILE": str(idx)}
    assert _plain_git(git, "read-tree", "HEAD", env=env).returncode == 0
    assert _plain_git(git, "rm", "--cached", "-q", "--", "doomed.txt", env=env).returncode == 0
    blocked = _plain_git(git, "commit", "-m", "delete without opt-in", env=env)
    assert blocked.returncode != 0 and "DELETE" in blocked.stderr

    ok = _plain_git(git, "commit", "-m", "delete on purpose",
                    env={**env, "ARCHON_HORIZON_ALLOW_DELETIONS": "1"})
    assert ok.returncode == 0, ok.stderr
    listing = _plain_git(git, "ls-tree", "--name-only", "HEAD")
    assert "doomed.txt" not in listing.stdout


def test_orchestrator_commit_still_records_deletions(tmp_path: Path) -> None:
    # Horizon's own integration commits legitimately record removals (e.g. a
    # file deleted from the worktree). ARCHON_COMMIT_BASE (fresh base) must let
    # them through without the ALLOW_DELETIONS escape hatch.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    (tmp_path / "gone.txt").write_text("tmp\n", "utf-8")
    assert git.commit("initial")
    (tmp_path / "gone.txt").unlink()
    sha = git.commit("removal")
    assert sha
    listing = _plain_git(git, "ls-tree", "--name-only", "HEAD")
    assert "gone.txt" not in listing.stdout
