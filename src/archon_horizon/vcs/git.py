"""The workspace's single out-of-tree git — the one source of truth for history.

The git model:

* The workspace has ONE repository, kept out-of-tree at
  ``.archon-horizon/vcs/workspace.git`` and driven via explicit ``--git-dir`` /
  ``--work-tree`` flags so it never creates a root ``.git`` that would collide
  with the user's own repo. It records a full snapshot per session: shared
  Horizon state plus the scoped project worktrees (not a manifest/lockfile —
  the actual files). There is no separate per-project repository; a project's
  history is just this repo filtered by pathspec (``git log -- <project>``).
* A project never carries a real ``.git/`` at its root (nested repos confuse
  parent-git). A project cloned with its own ``.git`` is neutralized (renamed
  aside) so its files, not a submodule gitlink, are committed.
* Structured provenance (run / round / role / session / task / projects) rides
  each commit as **git trailers**, so agents and the dashboard can query it
  deterministically instead of parsing prose. Computed metrics attach as **git
  notes**, which can be written/edited after the commit.

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


def _files_in_commit(git_dir: Path, work_tree: Path, sha: str) -> tuple[str, ...]:
    """The paths a commit touched — for a human-readable "what was committed" log.

    ``--root`` makes the initial commit (which has no parent) list its files too,
    rather than producing nothing. Best-effort: a lookup failure returns ``()``."""
    out = _run(
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha],
        git_dir=git_dir, work_tree=work_tree, check=False,
    )
    return tuple(line.strip() for line in out.splitlines() if line.strip())


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
        trailers: dict[str, str] | None = None,
        allow_empty: bool = False,
    ) -> str | None:
        """Stage and commit. Returns the new SHA, or None if nothing changed.

        ``author`` is an optional ``(name, email)`` that attributes the commit to
        the acting agent (Ground/Horizon) while the committer stays the system
        identity, so ``git log --author`` and blame distinguish the two.

        ``trailers`` are appended as machine-queryable ``Key: value`` lines at the
        end of the message (git-trailer convention), so provenance can be read
        back with ``git log --format=%(trailers:key=...)`` rather than parsed
        from prose. Empty values are dropped."""
        if trailers:
            message = message.rstrip() + "\n\n" + "".join(
                f"{key}: {value}\n" for key, value in trailers.items() if value
            )
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
        if not status and not allow_empty:
            return None
        args = ["commit", *_author_args(author)]
        if allow_empty:
            args.append("--allow-empty")
        self._run([*args, "-m", message])
        return self.current_sha()

    def current_sha(self) -> str | None:
        # --verify --quiet: prints nothing (instead of echoing "HEAD") and exits
        # non-zero when the branch is unborn, so an empty repo reads as None.
        sha = self._run(["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        return sha or None

    def files_in_commit(self, sha: str) -> tuple[str, ...]:
        return _files_in_commit(self.git_dir, self.root, sha)

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

    # ── diffing (backs the per-session change view) ──────────────────────
    #
    # ``base`` is the parent commit; pass None to diff against the empty tree
    # (a root commit). All are scoped by ``paths`` (workspace-relative), so a
    # session's change view covers exactly the projects the run could write.

    _EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's canonical empty tree

    def parent_sha(self, sha: str) -> str | None:
        """The first parent of ``sha``, or None if it is a root commit."""
        if not self.is_repo():
            return None
        out = self._run(["rev-parse", "--verify", "--quiet", f"{sha}^"], check=False)
        return out or None

    def session_commits(
        self,
        run_id: str,
        session: str,
        *,
        kinds: Sequence[str] | None = None,
    ) -> list[tuple[str, str]]:
        """``(sha, subject)`` for commits tagged with this run and session.

        ``kinds`` filters the ``Archon-Commit`` trailer (for example ``agent``
        or ``integration``). Without it this preserves the historical behavior:
        all commits for the session, oldest-first.
        """
        rows = self.session_commits_detailed(run_id, session)
        allowed = {k.strip().lower() for k in kinds or () if k.strip()}
        if allowed:
            rows = [r for r in rows if str(r.get("kind") or "").lower() in allowed]
        return [(str(r["sha"]), str(r["subject"])) for r in rows]

    def session_commits_detailed(self, run_id: str, session: str) -> list[dict[str, str]]:
        """Commit rows for one run/session, oldest-first, with provenance.

        ``Archon-Commit`` is the source-of-truth for new commits. Older ledgers
        did not have it, so infer the orchestrator integration sweep from its
        stable subject and treat other session commits as agent-authored.
        """
        if not self.is_repo() or not run_id or not session:
            return []
        # Keep the existing cheap git pre-filter; the trailer comparisons below
        # remain authoritative.
        out = self._run(
            ["log", "--fixed-strings", "--all-match",
             f"--grep=Archon-Run: {run_id}", f"--grep=Archon-Session: {session}",
             "--format=%H%x1f%s%x1f"
             "%(trailers:key=Archon-Run,valueonly,separator=%x1e)%x1f"
             "%(trailers:key=Archon-Session,valueonly,separator=%x1e)%x1f"
             "%(trailers:key=Archon-Role,valueonly,separator=%x1e)%x1f"
             "%(trailers:key=Archon-Commit,valueonly,separator=%x1e)"],
            check=False,
        ) or ""
        matched: list[dict[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 6:
                continue
            sha, subject, run_trailer, session_trailer, role_trailer, kind_trailer = parts
            runs = [v.strip() for v in run_trailer.split("\x1e") if v.strip()]
            sessions = [v.strip() for v in session_trailer.split("\x1e") if v.strip()]
            if run_id in runs and session in sessions:
                kinds = [v.strip().lower() for v in kind_trailer.split("\x1e") if v.strip()]
                kind = kinds[-1] if kinds else ("integration" if subject.startswith("workspace[") and ": integrate " in subject else "agent")
                roles = [v.strip().lower() for v in role_trailer.split("\x1e") if v.strip()]
                matched.append({
                    "sha": sha,
                    "subject": subject,
                    "role": roles[-1] if roles else "",
                    "kind": kind,
                })
        matched.reverse()  # oldest-first
        return matched

    def numstat(self, base: str | None, sha: str | None, paths: Sequence[str] = ()) -> list[tuple[int, int, str]]:
        """``(added, deleted, path)`` per changed file. A ``-`` count (binary) reads as 0.

        ``sha=None`` diffs against the current **working tree** (uncommitted
        state) — used for the live view of a running session that has not
        committed yet. ``-uall`` includes new untracked files individually."""
        if not self.is_repo():
            return []
        head = [base or self._EMPTY_TREE] + ([sha] if sha is not None else [])
        args = ["diff", "--numstat", *head]
        if sha is None:
            args = ["diff", "--numstat", base or self._EMPTY_TREE]
        if paths:
            args += ["--", *paths]
        rows: list[tuple[int, int, str]] = []
        # Include untracked files when diffing the working tree, so a brand-new
        # file the running session just created still shows up.
        untracked = self._worktree_untracked(paths) if sha is None else []
        for line in (self._run(args, check=False) or "").splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add = int(parts[0]) if parts[0].isdigit() else 0
            dele = int(parts[1]) if parts[1].isdigit() else 0
            rows.append((add, dele, parts[2].strip()))
        seen = {r[2] for r in rows}
        for path in untracked:
            if path not in seen:
                loc = len((self.root / path).read_text("utf-8", errors="ignore").splitlines()) if (self.root / path).is_file() else 0
                rows.append((loc, 0, path))
        return rows

    def _worktree_untracked(self, paths: Sequence[str] = ()) -> list[str]:
        args = ["ls-files", "--others", "--exclude-standard"]
        if paths:
            args += ["--", *paths]
        return [p for p in (self._run(args, check=False) or "").splitlines() if p]

    def diff(self, base: str | None, sha: str | None, paths: Sequence[str] = ()) -> str:
        """Unified diff text for ``base..sha`` scoped to ``paths`` (``sha=None`` →
        working tree). Untracked files are included when diffing the work tree."""
        if not self.is_repo():
            return ""
        if sha is None:
            args = ["diff", "--no-color", base or self._EMPTY_TREE]
        else:
            args = ["diff", "--no-color", base or self._EMPTY_TREE, sha]
        if paths:
            args += ["--", *paths]
        text = self._run(args, check=False) or ""
        if sha is None:  # append untracked files as full additions
            for path in self._worktree_untracked(paths):
                extra = self._run(["diff", "--no-color", "--no-index", "/dev/null", path], check=False)
                if extra:
                    text += ("\n" if text else "") + extra
        return text

    def file_at(self, sha: str | None, path: str) -> str | None:
        """Content of ``path`` at ``sha`` (``sha=None`` → current working tree),
        or None if it did not exist there."""
        if not self.is_repo():
            return None
        if sha is None:
            fp = self.root / path
            return fp.read_text("utf-8", errors="ignore") if fp.is_file() else None
        out = self._run(["show", f"{sha}:{path}"], check=False)
        # `git show` on a missing path prints nothing and exits non-zero; check=False
        # swallows the error, so an empty string is ambiguous. Confirm existence.
        listed = self._run(["ls-tree", "-r", "--name-only", sha, "--", path], check=False)
        return out if listed.strip() else None

    def add_note(self, sha: str, text: str) -> None:
        """Attach (overwrite) a git note on ``sha`` — a place for computed metrics
        that can be written after the commit without rewriting history."""
        if self.is_repo():
            self._run(["notes", "add", "-f", "-m", text, sha], check=False)

    def read_note(self, sha: str) -> str | None:
        if not self.is_repo():
            return None
        out = self._run(["notes", "show", sha], check=False)
        return out or None


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
    """Compatibility wrapper for project-scoped git operations.

    Newer workspace runs commit project files into the workspace repository, but
    setup and older callers still use this small wrapper when registering a
    VCS-enabled project.
    """

    def __init__(self, git_dir: Path, work_tree: Path) -> None:
        self.git_dir = git_dir
        self.work_tree = work_tree

    def is_repo(self) -> bool:
        return self.git_dir.exists()

    def init(self) -> None:
        if not self.is_repo():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            _init_bare(self.git_dir)
        neutralize_nested_git(self.work_tree)
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
        self.init()
        if self.current_sha():
            return None
        _run(["add", "-A"], git_dir=self.git_dir, work_tree=self.work_tree)
        _run(["commit", "--allow-empty", "-m", message], git_dir=self.git_dir, work_tree=self.work_tree)
        return self.current_sha()

    def current_sha(self) -> str | None:
        sha = _run(
            ["rev-parse", "--verify", "--quiet", "HEAD"],
            git_dir=self.git_dir,
            work_tree=self.work_tree,
            check=False,
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
    """Map each VCS-enabled project to its current SHA."""
    revisions: dict[str, str | None] = {}
    for name in workspace.projects:
        git = project_git_for(workspace, name)
        if git is not None and git.is_repo():
            revisions[name] = git.current_sha()
    return revisions
