"""Git model: the single out-of-tree workspace ledger."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from archon_horizon.vcs import git_available
from archon_horizon.vcs.git import WorkspaceGit

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _configure_identity(env_root: Path) -> None:
    # Commits need an author; set it locally inside each repo via env.
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


def test_workspace_ledger_excludes_entire_hgraph_tree(tmp_path: Path) -> None:
    # hgraph is regenerable / optional agent scratch — never agent source history.
    # Users who want it version it in their own root `.git`.
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    proj = tmp_path / "P"
    nodes = proj / "hgraph" / "nodes"
    edges = proj / "hgraph" / "edges"
    nodes.mkdir(parents=True)
    edges.mkdir(parents=True)
    (proj / "hgraph" / "config.yaml").write_text("lean: []\n", "utf-8")
    (nodes / "abcd1234ef00.md").write_text("---\ntitle: gen\n---\nbody\n", "utf-8")
    (edges / "a__b.md").write_text("---\ntype: uses\n---\n", "utf-8")
    comment_dir = nodes / "abcd1234ef00"
    comment_dir.mkdir()
    (comment_dir / "comment-1.md").write_text("authored note\n", "utf-8")
    (proj / "Foo.lean").write_text("def foo := 1\n", "utf-8")

    sha = git.commit("hgraph + lean", paths=["P"])
    assert sha
    files = set(git.files_in_commit(sha))
    assert "P/Foo.lean" in files
    assert not any("/hgraph/" in f or f.endswith("/hgraph") for f in files)


def test_commit_user_repo_paths_uses_root_dot_git(tmp_path: Path) -> None:
    from archon_horizon.vcs.git import commit_user_repo_paths, user_repo_git_dir

    _configure_identity(tmp_path)
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert user_repo_git_dir(tmp_path) is not None
    dash = tmp_path / "dashboard"
    dash.mkdir()
    (dash / "index.html").write_text("<html/>", "utf-8")
    sha = commit_user_repo_paths(
        tmp_path, "workspace: publish static dashboard", ["dashboard"]
    )
    assert sha
    tracked = subprocess.run(
        ["git", "ls-files", "dashboard/index.html"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "dashboard/index.html" in tracked
    # Second commit with no content change is a no-op.
    assert (
        commit_user_repo_paths(
            tmp_path, "workspace: publish static dashboard", ["dashboard"]
        )
        is None
    )


def test_workspace_git_does_not_track_horizon_state(tmp_path: Path) -> None:
    # Horizon control-plane state stays on disk for the live dashboard; the
    # agent ledger only journals sources (config + Lean/blueprint trees).
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    state_file = tmp_path / ".archon-horizon" / "events.jsonl"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}\n", "utf-8")
    (tmp_path / ".archon-horizon" / "inbox" / "local" / "items").mkdir(parents=True)
    (tmp_path / ".archon-horizon" / "inbox" / "local" / "items" / "I-1.yaml").write_text(
        "kind: hint\n", "utf-8"
    )

    sha = git.commit(
        "ledger",
        paths=["config.yaml", ".archon-horizon/events.jsonl", ".archon-horizon/inbox"],
    )
    assert sha
    files = set(git.files_in_commit(sha))
    assert "config.yaml" in files
    assert not any(f.startswith(".archon-horizon/") for f in files)


def test_ledger_prune_drops_historical_state_and_hgraph(tmp_path: Path) -> None:
    """Retro-clean: drop already-tracked state/hgraph without touching the work tree."""
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    lean = tmp_path / "P" / "Foo.lean"
    lean.parent.mkdir(parents=True)
    lean.write_text("def foo := 1\n", "utf-8")
    state = tmp_path / ".archon-horizon" / "events.jsonl"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{}\n", "utf-8")
    hgraph = tmp_path / "P" / "hgraph" / "nodes"
    hgraph.mkdir(parents=True)
    (hgraph / "abcd.md").write_text("gen\n", "utf-8")

    # Simulate an older ledger that force-tracked state + hgraph.
    ws = ["git", "--git-dir", str(git.git_dir), "--work-tree", str(tmp_path)]
    subprocess.run(ws + ["add", "-f", "--", "config.yaml", "P/Foo.lean",
                         ".archon-horizon/events.jsonl", "P/hgraph"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ws + ["commit", "-q", "-m", "bloat"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={**os.environ, "ARCHON_HORIZON_ALLOW_DELETIONS": "1"},
    )
    before = set(
        subprocess.run(ws + ["ls-files"], cwd=tmp_path, capture_output=True, text=True, check=True)
        .stdout.splitlines()
    )
    assert ".archon-horizon/events.jsonl" in before
    assert "P/hgraph/nodes/abcd.md" in before

    dry = git.prune_non_ledger_paths(dry_run=True)
    assert int(dry["paths"]) >= 2
    assert dry["sha"] is None

    summary = git.prune_non_ledger_paths()
    assert int(summary["paths"]) >= 2
    assert summary["sha"]
    after = set(
        subprocess.run(ws + ["ls-files"], cwd=tmp_path, capture_output=True, text=True, check=True)
        .stdout.splitlines()
    )
    assert "config.yaml" in after
    assert "P/Foo.lean" in after
    assert not any(p.startswith(".archon-horizon/") for p in after)
    assert not any("/hgraph/" in p for p in after)
    # Working tree untouched.
    assert state.exists() and hgraph.joinpath("abcd.md").exists()


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


def test_workspace_excludes_lock_tmp_and_process_markers(tmp_path: Path) -> None:
    """I-1913: volatile lock/tmp/process markers and full Horizon state stay out."""
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    state = tmp_path / ".archon-horizon"
    inbox = state / "inbox" / "local" / "items"
    inbox.mkdir(parents=True)
    (inbox / "I-0001.yaml").write_text("kind: hint\nstatus: open\nbody: |\n  t\n\n  d\n", "utf-8")
    (inbox / ".create.lock").write_text("1\n", "utf-8")
    (state / "inbox" / "local" / "items" / "I-0001.yaml.tmp").write_text("tmp\n", "utf-8")
    run = state / "runs" / "0001"
    run.mkdir(parents=True)
    (run / "process.json").write_text('{"pid":1}\n', "utf-8")
    (run / "run.yaml").write_text("id: '0001'\n", "utf-8")
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    (tmp_path / "_site" / "ops").mkdir(parents=True)
    (tmp_path / "_site" / "ops" / "index.html").write_text("<html/>\n", "utf-8")
    phase = tmp_path / "MainProjects" / ".phase0-pre-abc.XXXX"
    phase.mkdir(parents=True)
    (phase / "blob.bin").write_text("x" * 100, "utf-8")

    # Integration commits only stage config + project roots. State trees and
    # fully-ignored build/site dumps must not appear in the resulting commit.
    sha = git.commit(
        "state",
        paths=[
            "config.yaml",
            ".archon-horizon/inbox",
            ".archon-horizon/runs",
        ],
    )
    assert sha
    files = set(git.files_in_commit(sha))
    assert "config.yaml" in files
    assert not any(f.startswith(".archon-horizon/") for f in files)

    # Project-scoped add must also honour info/exclude for site/phase dumps.
    proj = tmp_path / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "Foo.lean").write_text("def foo := 1\n", "utf-8")
    (proj / ".lake" / "build").mkdir(parents=True)
    (proj / ".lake" / "build" / "x.olean").write_text("o", "utf-8")
    sha2 = git.commit("project", paths=["projects/p"])
    assert sha2
    files2 = set(git.files_in_commit(sha2))
    assert "projects/p/Foo.lean" in files2
    assert not any(".lake" in f for f in files2)
    assert not any(f.startswith("_site/") for f in files2)
    assert not any(".phase0-pre-abc" in f for f in files2)

    # A whole-tree add still leaves _site and phase snapshots untracked.
    assert git.commit("noise", paths=["_site", "MainProjects"]) is None


def test_commit_path_allowlist_rejects_outsider(tmp_path: Path) -> None:
    """I-0409: staged paths outside the explicit add set must be rejected."""
    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    assert git.commit("initial")

    # Seed a private index from HEAD, add only mine.txt, then sneak other.txt
    # into the index the way a concurrent broad-add would.
    idx = tmp_path / "allow-index"
    env = {"GIT_INDEX_FILE": str(idx)}
    assert _plain_git(git, "read-tree", "HEAD", env=env).returncode == 0
    (tmp_path / "mine.txt").write_text("mine\n", "utf-8")
    (tmp_path / "other.txt").write_text("other\n", "utf-8")
    assert _plain_git(git, "add", "--", "mine.txt", "other.txt", env=env).returncode == 0

    allow = tmp_path / "allow-paths"
    allow.write_text("mine.txt\n", "utf-8")
    blocked = _plain_git(
        git, "commit", "-m", "should block outsider",
        env={
            **env,
            "ARCHON_COMMIT_BASE": git.current_sha() or "",
            "ARCHON_COMMIT_PATHS_FILE": str(allow),
        },
    )
    assert blocked.returncode != 0
    assert "outside the explicit add set" in blocked.stderr
    assert "other.txt" in blocked.stderr

    # With both paths allowed, the commit proceeds.
    allow.write_text("mine.txt\nother.txt\n", "utf-8")
    ok = _plain_git(
        git, "commit", "-m", "both allowed",
        env={
            **env,
            "ARCHON_COMMIT_BASE": git.current_sha() or "",
            "ARCHON_COMMIT_PATHS_FILE": str(allow),
        },
    )
    assert ok.returncode == 0, ok.stderr


def test_status_porcelain_is_single_flight_and_cached(tmp_path: Path) -> None:
    from archon_horizon.vcs import git as git_mod
    from archon_horizon.vcs.git import status_porcelain

    _configure_identity(tmp_path)
    git = WorkspaceGit(tmp_path)
    git.init()
    (tmp_path / "config.yaml").write_text("x\n", "utf-8")
    assert git.commit("initial")
    (tmp_path / "config.yaml").write_text("y\n", "utf-8")

    calls = {"n": 0}
    real_run = git_mod._run

    def counting_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args") or []
        if cmd and "status" in cmd:
            calls["n"] += 1
        return real_run(*args, **kwargs)

    # Reset cache between tests.
    git_mod.invalidate_status_cache()
    import archon_horizon.vcs.git as g
    g._STATUS_CACHE.clear()

    # Patch at module level.
    original = git_mod._run
    git_mod._run = counting_run  # type: ignore[assignment]
    try:
        a = status_porcelain(git.git_dir, git.root, untracked="no", ttl_s=30.0)
        b = status_porcelain(git.git_dir, git.root, untracked="no", ttl_s=30.0)
        assert a == b
        assert "config.yaml" in a
        assert calls["n"] == 1  # second call served from cache
    finally:
        git_mod._run = original  # type: ignore[assignment]
        git_mod.invalidate_status_cache()
