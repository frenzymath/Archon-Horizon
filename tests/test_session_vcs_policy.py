"""Session VCS policy: project checkpoints, workspace integration, persistent locks."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from archon_horizon.config import operations
from archon_horizon.config.loader import build_workspace, load_config
from archon_horizon.core.tasks import WriteSet
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.vcs.git import WorkspaceGit, git_available
from archon_horizon.vcs.integration import integrate_workspace_baseline, integrate_workspace_session


pytestmark = pytest.mark.skipif(not git_available(), reason="git not installed")


def _identity() -> None:
    os.environ.setdefault("GIT_AUTHOR_NAME", "test")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "test@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "test")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "test@example.com")


def test_add_project_records_project_in_workspace_ledger(tmp_path: Path) -> None:
    _identity()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\nprojects: {}\n", "utf-8")

    operations.add_project(tmp_path, "p", "projects/p")

    cfg = load_config(tmp_path)
    build_workspace(cfg, tmp_path)  # config still loads
    # There is no per-project git any more: the single workspace ledger records
    # the registration commit instead.
    assert not (tmp_path / ".archon-horizon" / "vcs" / "p.git").exists()
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"
    assert ws_git_dir.exists()
    head = subprocess.run(
        ["git", "--git-dir", str(ws_git_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    assert head.returncode == 0 and head.stdout.strip()


def test_workspace_session_integration_commits_project_with_trailers(tmp_path: Path) -> None:
    _identity()
    project_dir = tmp_path / "projects" / "p"
    project_dir.mkdir(parents=True)
    workspace = Workspace(
        name="ws",
        root=tmp_path,
        projects={"p": Project(name="p", path=Path("projects/p"))},
    )
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    (project_dir / "Foo.lean").write_text("def foo := 1\n", "utf-8")

    integration = integrate_workspace_session(
        workspace,
        run_id="0001",
        session="0001-horizon-T-1",
        role="horizon",
        round_index=1,
        project="p",
        task_id="T-1",
        projects=("p",),
    )

    # The session is committed straight into the single workspace ledger.
    assert integration.workspace_commit and integration.workspace_commit_error is None
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"
    tracked = subprocess.run(
        ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
         "ls-files", "projects/p/Foo.lean"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "projects/p/Foo.lean" in tracked
    assert not (tmp_path / "projects" / "p" / ".git").exists()
    assert not (tmp_path / ".git").exists()  # workspace git is out-of-tree
    assert not (tmp_path / ".archon-horizon" / "vcs" / "p.git").exists()  # no per-project git

    # The commit subject encodes the step, provenance rides as git trailers (so
    # a session maps to its commit deterministically), and the author is the
    # acting agent (committer stays the system identity).
    show = subprocess.run(
        ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
         "show", "-s", "--format=%an%n%s", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    author_line, subject = show.split("\n", 1)
    assert author_line == "Archon Horizon (Horizon)"
    assert subject.strip() == "workspace[0001 r1] horizon T-1: integrate 0001-horizon-T-1"

    def _trailer(key: str) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
             "show", "-s", f"--format=%(trailers:key={key},valueonly)", "HEAD"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()

    assert _trailer("Archon-Run") == "0001"
    assert _trailer("Archon-Round") == "1"
    assert _trailer("Archon-Role") == "horizon"
    assert _trailer("Archon-Session") == "0001-horizon-T-1"
    assert _trailer("Archon-Task") == "T-1"
    assert _trailer("Archon-Projects") == "p"


def test_workspace_baseline_commit_is_created_at_run_start(tmp_path: Path) -> None:
    _identity()
    project_dir = tmp_path / "projects" / "p"
    project_dir.mkdir(parents=True)
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    workspace = Workspace(
        name="ws",
        root=tmp_path,
        projects={"p": Project(name="p", path=Path("projects/p"))},
    )

    outcome = integrate_workspace_baseline(workspace, run_id="0007", projects=("p",))

    assert outcome.attempted is True
    assert outcome.error is None
    assert outcome.sha
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"

    def _show(format_spec: str) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path), "show", "-s", f"--format={format_spec}", "HEAD"],
            cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()

    assert _show("%an") == "Archon Horizon (System)"
    assert _show("%s") == "workspace[0007] system: baseline"
    assert _show("%(trailers:key=Archon-Run,valueonly)") == "0007"
    assert _show("%(trailers:key=Archon-Session,valueonly)") == "run-baseline"
    assert _show("%(trailers:key=Archon-Commit,valueonly)") == "baseline"


def test_workspace_integration_excludes_build_and_nested_git_artifacts(tmp_path: Path) -> None:
    """The ledger must not swallow Lean build output (.lake/.olean) or a disabled
    nested git — the cause of the multi-hundred-MB push. Even pre-existing tracked
    artifacts (from the old force-add) are pruned on the next integration."""
    from archon_horizon.vcs.integration import integrate_workspace_run

    _identity()
    project_dir = tmp_path / "projects" / "p"
    (project_dir / ".lake" / "build").mkdir(parents=True)
    (project_dir / ".git.disabled" / "objects").mkdir(parents=True)
    (project_dir / "Foo.lean").write_text("def foo := 1\n", "utf-8")
    (project_dir / ".lake" / "build" / "Foo.olean").write_text("BIG", "utf-8")
    (project_dir / ".git.disabled" / "objects" / "p.pack").write_text("PACK", "utf-8")
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    workspace = Workspace(
        name="ws", root=tmp_path,
        projects={"p": Project(name="p", path=Path("projects/p"))},
    )
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"

    # Simulate the OLD bug: force-add the whole project tree into the ledger.
    WorkspaceGit(tmp_path).init()
    subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                    "add", "-f", "--", "projects/p"], cwd=tmp_path, check=True)
    subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                    "commit", "-q", "-m", "bloat"], cwd=tmp_path, check=True)

    integrate_workspace_run(workspace, run_id="0001", projects=("p",))

    tracked = subprocess.run(
        ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path), "ls-files"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "projects/p/Foo.lean" in tracked          # real source kept
    assert ".lake" not in tracked                     # build output pruned + excluded
    assert ".olean" not in tracked
    assert ".git.disabled" not in tracked             # disabled nested git excluded
    # The secret-guard hook is installed and executable.
    hook = ws_git_dir / "hooks" / "pre-commit"
    assert hook.exists() and (hook.stat().st_mode & 0o111)


def test_nested_git_project_is_committed_as_files_not_gitlink(tmp_path: Path) -> None:
    """A project cloned with its own in-tree .git must land in the ledger as its
    files (a tree), not a submodule gitlink (mode 160000)."""
    from archon_horizon.vcs.integration import integrate_workspace_run

    _identity()
    project_dir = tmp_path / "projects" / "leheng"
    project_dir.mkdir(parents=True)
    (project_dir / "Ch0.lean").write_text("def ch0 := 0\n", "utf-8")
    # Give it a real nested repo (the cause of the gitlink).
    subprocess.run(["git", "init", "-q", str(project_dir)], check=True)
    assert (project_dir / ".git").is_dir()

    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    workspace = Workspace(
        name="ws", root=tmp_path,
        projects={"leheng": Project(name="leheng", path=Path("projects/leheng"))},
    )

    integrate_workspace_run(workspace, run_id="0001", projects=("leheng",))

    # The nested .git was renamed aside, not committed.
    assert not (project_dir / ".git").is_dir()
    assert (project_dir / ".git.disabled").is_dir()
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"
    entry = subprocess.run(
        ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path), "ls-tree", "HEAD", "projects/leheng"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "040000 tree" in entry          # a real directory tree…
    assert "160000 commit" not in entry    # …not a submodule gitlink
    tracked = subprocess.run(
        ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path), "ls-files", "projects/leheng"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "projects/leheng/Ch0.lean" in tracked
    assert ".git.disabled" not in tracked  # the disabled repo is excluded


def test_existing_gitlink_is_converted_to_tracked_files(tmp_path: Path) -> None:
    """A project already committed as a submodule gitlink (before its nested .git
    was neutralized) is converted to its files on the next integration."""
    from archon_horizon.vcs.integration import integrate_workspace_run

    _identity()
    project_dir = tmp_path / "projects" / "leheng"
    project_dir.mkdir(parents=True)
    (project_dir / "Ch0.lean").write_text("def ch0 := 0\n", "utf-8")
    subprocess.run(["git", "init", "-q", str(project_dir)], check=True)
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"

    # Simulate the OLD state: a committed gitlink for the project.
    subprocess.run(["git", "-C", str(project_dir), "-c", "user.email=t@e", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "x"], check=True)
    WorkspaceGit(tmp_path).init()
    subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                    "-c", "protocol.file.allow=always", "add", "projects/leheng"],
                   cwd=tmp_path, check=True)
    subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                    "commit", "-q", "-m", "gitlink"], cwd=tmp_path, check=True)
    entry = subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                            "ls-tree", "HEAD", "projects/leheng"], cwd=tmp_path,
                           capture_output=True, text=True, check=True).stdout
    assert "160000 commit" in entry  # precondition: it is a gitlink

    workspace = Workspace(
        name="ws", root=tmp_path,
        projects={"leheng": Project(name="leheng", path=Path("projects/leheng"))},
    )
    integrate_workspace_run(workspace, run_id="0001", projects=("leheng",))

    entry = subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                            "ls-tree", "HEAD", "projects/leheng"], cwd=tmp_path,
                           capture_output=True, text=True, check=True).stdout
    assert "040000 tree" in entry and "160000 commit" not in entry
    tracked = subprocess.run(["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path),
                              "ls-files", "projects/leheng"], cwd=tmp_path,
                             capture_output=True, text=True, check=True).stdout
    assert "projects/leheng/Ch0.lean" in tracked


def _commit_leak(tmp_path: Path, body: str, *, env: dict | None = None):
    """Stage a file with ``body`` and commit it through the guarded ledger git.
    Returns (CompletedProcess, committed_content)."""
    _identity()
    (tmp_path / "config.yaml").write_text("workspace: {name: ws}\n", "utf-8")
    (tmp_path / ".archon-horizon").mkdir(exist_ok=True)
    ws_git_dir = tmp_path / ".archon-horizon" / "vcs" / "workspace.git"
    WorkspaceGit(tmp_path).init()
    (tmp_path / ".archon-horizon" / "leak.txt").write_text(body, "utf-8")
    base = ["git", "--git-dir", str(ws_git_dir), "--work-tree", str(tmp_path)]
    subprocess.run(base + ["add", "-f", ".archon-horizon/leak.txt"], cwd=tmp_path, check=True)
    proc = subprocess.run(base + ["commit", "-m", "x"], cwd=tmp_path,
                          capture_output=True, text=True, env=env)
    committed = subprocess.run(base + ["show", "HEAD:.archon-horizon/leak.txt"],
                               cwd=tmp_path, capture_output=True, text=True).stdout
    return proc, committed


def test_secret_guard_redacts_and_never_blocks(tmp_path: Path) -> None:
    """A credential is REDACTED to XXXX in the committed (and working-tree) content
    and the commit still succeeds — the guard never blocks."""
    secret = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    proc, committed = _commit_leak(tmp_path, f'token = "{secret}"\n')
    assert proc.returncode == 0                     # never blocked
    assert "redacted" in proc.stderr.lower()
    assert secret not in committed and "XXXX" in committed
    # the working-tree file is redacted too, so the secret does not linger
    assert secret not in (tmp_path / ".archon-horizon" / "leak.txt").read_text("utf-8")


def test_secret_guard_override_preserves_content(tmp_path: Path) -> None:
    """ARCHON_HORIZON_ALLOW_SECRETS=1 skips the scan: content is committed verbatim."""
    import os as _os

    secret = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    proc, committed = _commit_leak(
        tmp_path, f'token = "{secret}"\n',
        env={**_os.environ, "ARCHON_HORIZON_ALLOW_SECRETS": "1"},
    )
    assert proc.returncode == 0
    assert secret in committed  # not redacted when explicitly allowed


def test_secret_guard_no_false_positive_case_sensitive(tmp_path: Path) -> None:
    """A lowercase camelCase identifier that a case-insensitive scan would flag
    (AKIA…) must be left untouched now that the scan is case-sensitive (I-0238)."""
    ident = "def akiaLongCamelCaseIdentifier01 := 1"
    proc, committed = _commit_leak(tmp_path, ident + "\n")
    assert proc.returncode == 0
    assert "XXXX" not in committed
    assert ident in committed


