"""Phase 0 (v0.1.1): the agent records work with plain `git` into the workspace
ledger; a prepare-commit-msg hook stamps provenance trailers from the session env,
so a raw commit still maps to its session/task without the `horizon commit` wrapper.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from archon_horizon.vcs.git import WorkspaceGit, git_available, install_ledger_git_wrapper

pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _session_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "ARCHON_HORIZON_RUN": "run-1",
            "ARCHON_HORIZON_AGENT_ROLE": "horizon",
            "ARCHON_HORIZON_SESSION": "sess-1",
            "ARCHON_HORIZON_TASK": "T-1",
            "ARCHON_HORIZON_PROJECTS": "p",
        }
    )
    # A caller can clear a provenance var by passing it as "" (drop from env).
    for key, value in overrides.items():
        if value == "":
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _ledger_git(root: Path, *args: str, env: dict[str, str], cwd: Path | None = None) -> str:
    gd = WorkspaceGit(root).git_dir
    out = subprocess.run(
        ["git", f"--git-dir={gd}", f"--work-tree={root}", *args],
        cwd=str(cwd or root), env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout


def _commit_message(root: Path, env: dict[str, str]) -> str:
    return _ledger_git(root, "log", "-1", "--format=%B", env=env)


def _make_commit(root: Path, name: str, body: str, message: str, env: dict[str, str]) -> None:
    (root / name).write_text(body, "utf-8")
    _ledger_git(root, "add", name, env=env)
    _ledger_git(root, "commit", "-m", message, env=env)


def test_hook_stamps_provenance_trailers_on_raw_commit(tmp_path: Path) -> None:
    WorkspaceGit(tmp_path).init()
    env = _session_env()

    _make_commit(tmp_path, "a.txt", "hi", "prove foo", env)

    msg = _commit_message(tmp_path, env)
    assert "prove foo" in msg
    assert "Archon-Run: run-1" in msg
    assert "Archon-Role: horizon" in msg
    assert "Archon-Session: sess-1" in msg
    assert "Archon-Task: T-1" in msg
    assert "Archon-Projects: p" in msg
    assert "Archon-Commit: agent" in msg


def test_hook_is_idempotent_when_message_already_stamped(tmp_path: Path) -> None:
    WorkspaceGit(tmp_path).init()
    env = _session_env()

    # A message that already carries an Archon-Run trailer (e.g. the Python
    # integration path stamped it) must not be doubled.
    _make_commit(tmp_path, "a.txt", "hi", "prove foo\n\nArchon-Run: preexisting", env)

    msg = _commit_message(tmp_path, env)
    assert msg.count("Archon-Run:") == 1
    assert "Archon-Run: preexisting" in msg
    assert "Archon-Run: run-1" not in msg
    # Since the message was already stamped, the hook adds nothing else either.
    assert "Archon-Commit: agent" not in msg


def test_hook_is_noop_outside_a_session(tmp_path: Path) -> None:
    WorkspaceGit(tmp_path).init()
    env = _session_env(ARCHON_HORIZON_RUN="")  # not inside a Horizon session

    _make_commit(tmp_path, "a.txt", "hi", "manual commit", env)

    msg = _commit_message(tmp_path, env)
    assert "Archon-Run" not in msg
    assert "Archon-Commit" not in msg


def test_hgit_wrapper_commits_to_the_ledger_with_provenance(tmp_path: Path) -> None:
    WorkspaceGit(tmp_path).init()
    wrapper = install_ledger_git_wrapper(tmp_path / ".archon-horizon")
    assert wrapper is not None and os.access(wrapper, os.X_OK)

    env = _session_env()
    env["HORIZON_LEDGER_GIT_DIR"] = str(WorkspaceGit(tmp_path).git_dir)
    env["HORIZON_LEDGER_WORK_TREE"] = str(tmp_path)

    (tmp_path / "b.txt").write_text("via hgit", "utf-8")
    subprocess.run([str(wrapper), "add", "b.txt"], cwd=str(tmp_path), env=env, check=True)
    subprocess.run([str(wrapper), "commit", "-m", "build bar"], cwd=str(tmp_path), env=env, check=True)

    msg = _commit_message(tmp_path, env)
    assert "build bar" in msg
    assert "Archon-Session: sess-1" in msg
    assert "Archon-Commit: agent" in msg


def test_ledger_bin_is_excluded_from_commits(tmp_path: Path) -> None:
    WorkspaceGit(tmp_path).init()
    install_ledger_git_wrapper(tmp_path / ".archon-horizon")
    env = _session_env()

    # A broad `add -A` must not pull the auto-installed wrapper into the ledger.
    (tmp_path / "c.txt").write_text("real work", "utf-8")
    _ledger_git(tmp_path, "add", "-A", env=env)
    _ledger_git(tmp_path, "commit", "-m", "work", env=env)

    tracked = _ledger_git(tmp_path, "ls-files", env=env)
    assert "c.txt" in tracked
    assert ".archon-horizon/bin/hgit" not in tracked
    assert "bin/hgit" not in tracked
