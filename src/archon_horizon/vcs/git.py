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

import os
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
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
    )
    if check and completed.returncode != 0:
        raise GitError(f"{' '.join(cmd)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _author_args(author: tuple[str, str] | None) -> list[str]:
    """``--author`` flag for ``git commit``, or nothing when unset."""
    if not author:
        return []
    name, email = author
    return [f"--author={name} <{email}>"]


# The default branch for every repo Archon Horizon creates. We pin it explicitly
# (rather than inheriting git's ``init.defaultBranch``, which is still ``master``
# on unconfigured installs) so the shared ledger and project journals are
# consistent — and consistent with the sibling Archon project. We only set it at
# creation time, so a user who later switches a project/workspace repo to another
# branch keeps committing on their branch: nothing here ever checks out or resets.
_DEFAULT_BRANCH = "main"


def _init_bare(git_dir: Path) -> None:
    """``git init --bare`` on the :data:`_DEFAULT_BRANCH`.

    ``--initial-branch`` needs git ≥ 2.28; on older git we fall back to a plain
    init and then point the unborn ``HEAD`` at ``main`` via ``symbolic-ref`` (safe
    because the repo has no commits yet)."""
    try:
        _run(["init", "--bare", f"--initial-branch={_DEFAULT_BRANCH}", str(git_dir)])
    except GitError:
        _run(["init", "--bare", str(git_dir)])
        _run(["symbolic-ref", "HEAD", f"refs/heads/{_DEFAULT_BRANCH}"], git_dir=git_dir, check=False)


# Patterns the out-of-tree gits must ignore. The work tree is the user's
# project / workspace, so these keep build artifacts, caches, nested git repos,
# and secrets out of both the project journals and the shared ledger. The
# project's own .gitignore is honoured too; this is the floor that applies even
# when a project ships no .gitignore (the cause of the .lake / .git.disabled
# bloat). Auto-managed: rewritten on every init so existing workspaces self-heal.
_COMMON_EXCLUDES = (
    "# Auto-managed by Archon Horizon — do not edit; regenerated on each run.",
    "# Lean / build artifacts",
    ".lake/", "*.olean", "*.ilean", "lake-packages/",
    "# Regenerable agent-run logs (Archon tooling)",
    ".logis/",
    "# Disabled or nested git repositories",
    ".git.disabled/", "*.git.disabled/",
    "# Language caches / deps",
    "__pycache__/", "*.pyc", ".venv/", "venv/", "*.egg-info/",
    ".mypy_cache/", ".pytest_cache/", ".ruff_cache/", "node_modules/", ".cache/",
    "# OS / editor / local AI tooling",
    ".DS_Store", ".idea/", ".vscode/", ".claude/",
    "# Secrets — never commit credentials",
    ".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519",
    "*.p12", "*.pfx", ".netrc", "*.secret", "secrets.yaml", "secrets.yml",
)

# Workspace-ledger-only excludes: the project git dirs and ephemeral leases that
# live under the workspace root (a project work tree never contains these).
_WORKSPACE_EXCLUDES = (".archon-horizon/vcs/", ".archon-horizon/locks/")

# A pre-commit guard installed into every out-of-tree git so an accidental
# credential (in a transcript, config, or dropped file) is caught before it is
# committed. High-confidence formats only, to avoid blocking ordinary content;
# set ARCHON_HORIZON_ALLOW_SECRETS=1 to bypass in a pinch.
_SECRET_HOOK = r"""#!/bin/sh
# Auto-installed by Archon Horizon. Blocks commits introducing obvious secrets.
[ "$ARCHON_HORIZON_ALLOW_SECRETS" = "1" ] && exit 0
added=$(git diff --cached --no-color -U0 --diff-filter=AM 2>/dev/null | grep '^+' | grep -v '^+++')
hit=$(printf '%s\n' "$added" | grep -Ein \
  'ghp_[0-9A-Za-z]{30,}|gho_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,}|sk-ant-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----')
if [ -n "$hit" ]; then
  echo "Archon Horizon: possible secret in staged changes; commit blocked." >&2
  echo "Remove it, or set ARCHON_HORIZON_ALLOW_SECRETS=1 to override." >&2
  exit 1
fi
exit 0
"""


def neutralize_nested_git(work_tree: Path) -> str | None:
    """Rename a project's in-tree ``.git`` directory to ``.git.disabled``.

    Archon Horizon gives each project an *out-of-tree* git, so an in-tree ``.git``
    is an anomaly: git would record the project as a submodule **gitlink** (mode
    160000 — a bare commit pointer) instead of committing its files. Renaming the
    nested repo aside (it is then covered by the ``.git.disabled/`` exclude) makes
    the ledger store the actual files. Returns the new path if it renamed, else
    ``None``. Idempotent; never deletes data (skips if ``.git.disabled`` exists)."""
    dot = work_tree / ".git"
    if not dot.is_dir():  # absent, or a gitfile/worktree pointer — leave alone
        return None
    disabled = work_tree / ".git.disabled"
    if disabled.exists():
        return None
    dot.rename(disabled)
    return str(disabled)


def _ensure_repo_hygiene(git_dir: Path, *, extra_excludes: Sequence[str] = ()) -> None:
    """(Re)write ``info/exclude`` and install the secret-guard ``pre-commit`` hook
    for an out-of-tree git. Idempotent and cheap; run on every ``init`` so an
    existing workspace picks up new rules without manual migration."""
    info = git_dir / "info"
    info.mkdir(parents=True, exist_ok=True)
    lines = [*_COMMON_EXCLUDES, *extra_excludes]
    (info / "exclude").write_text("\n".join(lines) + "\n", "utf-8")
    hooks = git_dir / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-commit"
    hook.write_text(_SECRET_HOOK, "utf-8")
    hook.chmod(0o755)


def _prune_ignored_from_index(git_dir: Path, work_tree: Path) -> None:
    """Drop already-tracked but now-ignored paths from the index (e.g. ``.lake``
    committed before the excludes existed), so the next commit records their
    removal. Touches only the index (``--cached``); the working tree is left
    intact. Self-heals a workspace that was bloated by the old force-add."""
    listed = _run(
        ["ls-files", "-z", "-ci", "--exclude-standard"],
        git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False,
    )
    paths = [p for p in listed.split("\0") if p]
    if not paths:
        return
    # Batch to stay well under ARG_MAX on large trees (thousands of .lake files).
    for start in range(0, len(paths), 500):
        _run(
            ["rm", "--cached", "-q", "--", *paths[start:start + 500]],
            git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False,
        )


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Archon Horizon")
    env.setdefault("GIT_AUTHOR_EMAIL", "archon-horizon@local")
    env.setdefault("GIT_COMMITTER_NAME", "Archon Horizon")
    env.setdefault("GIT_COMMITTER_EMAIL", "archon-horizon@local")
    return env


class WorkspaceGit:
    """The workspace's own repository (the manifest root).

    Kept **out-of-tree** at ``.archon-horizon/vcs/workspace.git`` and driven via
    ``--git-dir`` / ``--work-tree``, so it never creates a root ``.git`` that
    would collide with — or commit into — the user's own repository if they run
    Archon Horizon inside one. The work tree is the workspace root.
    """

    def __init__(self, root: Path, git_dir: Path | None = None) -> None:
        self.root = root
        self.git_dir = git_dir or (root / ".archon-horizon" / "vcs" / "workspace.git")

    def _run(self, args: Sequence[str], *, check: bool = True) -> str:
        # cwd=root so relative pathspecs resolve against the work tree; the
        # explicit --git-dir means git never discovers a parent .git.
        return _run(args, git_dir=self.git_dir, work_tree=self.root, cwd=self.root, check=check)

    def is_repo(self) -> bool:
        return self.git_dir.exists()

    def init(self) -> None:
        if not self.is_repo():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            _init_bare(self.git_dir)
        # Always refresh excludes + secret hook so existing workspaces self-heal,
        # then drop any now-ignored paths a previous force-add had tracked.
        _ensure_repo_hygiene(self.git_dir, extra_excludes=_WORKSPACE_EXCLUDES)
        _prune_ignored_from_index(self.git_dir, self.root)

    def commit(
        self,
        message: str,
        paths: Sequence[str] | None = None,
        *,
        author: tuple[str, str] | None = None,
    ) -> str | None:
        """Stage and commit. Returns the new SHA, or None if nothing changed.

        ``author`` is an optional ``(name, email)`` that attributes the commit to
        the acting agent (Ground/Horizon) while the committer stays the system
        identity, so ``git log --author`` and blame distinguish the two."""
        if paths is None:
            self._run(["add", "-A"])
        else:
            # `.archon-horizon` state and config.yaml are force-added: the user's
            # own root .gitignore may exclude them, but the ledger must record
            # them. Project trees are added WITHOUT force, so the excludes and the
            # project's .gitignore apply — keeping .lake / .olean / .git.disabled
            # out of the ledger instead of force-committing the whole tree.
            state = [p for p in paths if p == "config.yaml" or p.split("/", 1)[0] == ".archon-horizon"]
            projects = [p for p in paths if p not in state]
            if state:
                self._run(["add", "-f", "--", *state])
            if projects:
                self._run(["add", "-A", "--", *projects])
        status = self._run(["status", "--porcelain"])
        if not status:
            return None
        self._run(["commit", *_author_args(author), "-m", message])
        return self.current_sha()

    def current_sha(self) -> str | None:
        # --verify --quiet: prints nothing (instead of echoing "HEAD") and exits
        # non-zero when the branch is unborn, so an empty repo reads as None.
        sha = self._run(["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        return sha or None

    def unstage_gitlink(self, path: str) -> bool:
        """If ``path`` is staged as a submodule gitlink (mode 160000) but is now a
        plain directory on disk, drop the gitlink so a normal ``add`` re-tracks its
        files. Converts an already-committed gitlink (from before the nested .git
        was neutralized) into a real tree. Returns True if it dropped one."""
        listing = self._run(["ls-files", "-s", "--", path], check=False)
        if not listing.startswith("160000"):
            return False
        self._run(["rm", "--cached", "-q", "--ignore-unmatch", "--", path], check=False)
        return True

    def changed_files(self) -> tuple[str, ...]:
        """Workspace-relative paths with uncommitted changes (porcelain)."""
        if not self.is_repo():
            return ()
        # -uall lists untracked files individually instead of collapsing a
        # fully-untracked directory to "dir/", which would hide the filenames.
        status = self._run(["status", "--porcelain", "-uall"], check=False) or ""
        return _parse_porcelain(status)


def _parse_porcelain(status: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        entry = line[3:] if len(line) > 3 else line.strip()
        if " -> " in entry:  # renames: "old -> new"
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip().strip('"'))
    return tuple(paths)


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
            _init_bare(self.git_dir)
        # A nested in-tree .git would be stored as a submodule gitlink; rename it
        # aside so the project's files are committed instead.
        neutralize_nested_git(self.work_tree)
        # Always refresh excludes + secret hook (self-heals existing repos), then
        # drop any now-ignored paths a prior checkpoint had tracked (.lake, etc.).
        _ensure_repo_hygiene(self.git_dir)
        _prune_ignored_from_index(self.git_dir, self.work_tree)

    def commit(self, message: str, *, author: tuple[str, str] | None = None) -> str | None:
        _run(["add", "-A"], git_dir=self.git_dir, work_tree=self.work_tree)
        status = _run(["status", "--porcelain"], git_dir=self.git_dir, work_tree=self.work_tree)
        if not status:
            return None
        _run(["commit", *_author_args(author), "-m", message], git_dir=self.git_dir, work_tree=self.work_tree)
        return self.current_sha()

    def ensure_initial_commit(self, message: str = "project: baseline (registered)") -> str | None:
        """Give a freshly-initialized project repo a first commit so its tree is
        tracked from registration — not stuck on an empty branch with no HEAD
        (the "no commits yet on main" state). No-op if history already exists."""
        self.init()
        if self.current_sha():
            return None
        _run(["add", "-A"], git_dir=self.git_dir, work_tree=self.work_tree)
        _run(["commit", "--allow-empty", "-m", message], git_dir=self.git_dir, work_tree=self.work_tree)
        return self.current_sha()

    def current_sha(self) -> str | None:
        # --verify --quiet: prints nothing (instead of echoing "HEAD") and exits
        # non-zero when the branch is unborn, so an empty repo reads as None.
        sha = _run(
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            git_dir=self.git_dir, work_tree=self.work_tree, check=False,
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
