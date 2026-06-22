"""Git wrappers for the workspace manifest and out-of-tree project VCS.

The roadmap's git model:

* The workspace has its own repo and acts as a *manifest* — it records the
  set of project revisions (like a lockfile), not a copy of every project
  commit.
* A project never carries a real ``.git/`` at its root (nested repos confuse
  parent-git). If a project needs history, its git directory lives at
  ``.archon-horizon/vcs/<project>.git`` and is driven via explicit
  ``--git-dir`` / ``--work-tree`` flags, leaving the project tree an ordinary
  embedded directory.

Everything here shells out to ``git`` and degrades gracefully when ``git`` is
absent (``git_available()`` is False; constructors still build).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from archon_horizon.core.workspace import Workspace


class GitError(RuntimeError):
    pass


def git_available() -> bool:
    return shutil.which("git") is not None


def _run(
    args: Sequence[str],
    *,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    cwd: Path | None = None,
    check: bool = True,
) -> str:
    cmd = ["git"]
    if git_dir is not None:
        cmd += ["--git-dir", str(git_dir)]
    if work_tree is not None:
        cmd += ["--work-tree", str(work_tree)]
    cmd += list(args)
    completed = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if check and completed.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


class WorkspaceGit:
    """The workspace's own repository (the manifest root)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def is_repo(self) -> bool:
        return (self.root / ".git").exists()

    def init(self) -> None:
        if not self.is_repo():
            _run(["init"], cwd=self.root)

    def commit(self, message: str, paths: Sequence[str] | None = None) -> str | None:
        """Stage and commit. Returns the new SHA, or None if nothing changed."""
        _run(["add", *(paths or ["-A"])], cwd=self.root)
        status = _run(["status", "--porcelain"], cwd=self.root)
        if not status:
            return None
        _run(["commit", "-m", message], cwd=self.root)
        return self.current_sha()

    def current_sha(self) -> str | None:
        sha = _run(["rev-parse", "HEAD"], cwd=self.root, check=False)
        return sha or None


class ProjectGit:
    """A project's out-of-tree git, driven via --git-dir / --work-tree."""

    def __init__(self, git_dir: Path, work_tree: Path) -> None:
        self.git_dir = git_dir
        self.work_tree = work_tree

    def is_repo(self) -> bool:
        return self.git_dir.exists()

    def init(self) -> None:
        if not self.is_repo():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            _run(["init", "--bare", str(self.git_dir)])

    def commit(self, message: str) -> str | None:
        _run(["add", "-A"], git_dir=self.git_dir, work_tree=self.work_tree)
        status = _run(["status", "--porcelain"], git_dir=self.git_dir, work_tree=self.work_tree)
        if not status:
            return None
        _run(["commit", "-m", message], git_dir=self.git_dir, work_tree=self.work_tree)
        return self.current_sha()

    def current_sha(self) -> str | None:
        sha = _run(
            ["rev-parse", "HEAD"], git_dir=self.git_dir, work_tree=self.work_tree, check=False
        )
        return sha or None


def project_git_for(workspace: Workspace, name: str) -> ProjectGit | None:
    """Build a :class:`ProjectGit` for a VCS-enabled project, else None."""
    project = workspace.project(name)
    if not project.vcs.enabled:
        return None
    git_dir = project.vcs.git_dir or (workspace.state_path / "vcs" / f"{name}.git")
    if not git_dir.is_absolute():
        git_dir = workspace.root / git_dir
    return ProjectGit(git_dir=git_dir, work_tree=workspace.project_path(name))


def collect_revisions(workspace: Workspace) -> dict[str, str | None]:
    """Map each VCS-enabled project to its current SHA — the manifest payload."""
    revisions: dict[str, str | None] = {}
    for name in workspace.projects:
        git = project_git_for(workspace, name)
        if git is not None and git.is_repo():
            revisions[name] = git.current_sha()
    return revisions
