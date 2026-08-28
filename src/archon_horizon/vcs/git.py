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
  deterministically instead of parsing prose.

Everything here shells out to ``git`` and degrades gracefully when ``git`` is
absent (``git_available()`` is False; constructors still build).
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path



class GitError(RuntimeError):
    pass


# Single-flight porcelain status per (git_dir, work_tree, untracked mode).
# Mature workspaces have tens of thousands of untracked paths; concurrent
# dashboards/hooks/agents each launching `git status` can pin the machine for
# minutes. A short-lived shared snapshot collapses the stampede.
_STATUS_LOCK = threading.Lock()
_STATUS_INFLIGHT: dict[tuple[str, str, str], threading.Event] = {}
_STATUS_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
_STATUS_TTL_S = 2.0


def status_porcelain(
    git_dir: Path,
    work_tree: Path,
    *,
    untracked: str = "no",
    ttl_s: float = _STATUS_TTL_S,
) -> str:
    """Return ``git status --porcelain`` text, single-flight + briefly cached.

    ``untracked`` is one of git's ``--untracked-files`` modes (``no`` /
    ``normal`` / ``all``). Default ``no`` keeps large worktrees responsive.
    """
    mode = untracked if untracked in {"no", "normal", "all"} else "no"
    key = (str(Path(git_dir).resolve()), str(Path(work_tree).resolve()), mode)
    now = time.monotonic()
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(key)
        if cached is not None and now - cached[0] < ttl_s:
            return cached[1]
        inflight = _STATUS_INFLIGHT.get(key)
        if inflight is None:
            inflight = threading.Event()
            _STATUS_INFLIGHT[key] = inflight
            owner = True
        else:
            owner = False
    if not owner:
        # Wait for the owner; fall through to cache (or empty on failure).
        inflight.wait(timeout=max(ttl_s * 10, 30.0))
        with _STATUS_LOCK:
            cached = _STATUS_CACHE.get(key)
            return cached[1] if cached is not None else ""
    try:
        text = _run(
            [
                "-c", "core.quotepath=false",
                "status", "--porcelain=v1",
                f"--untracked-files={mode}",
            ],
            git_dir=Path(key[0]),
            work_tree=Path(key[1]),
            cwd=Path(key[1]),
            check=False,
        ) or ""
    except GitError:
        text = ""
    with _STATUS_LOCK:
        _STATUS_CACHE[key] = (time.monotonic(), text)
        _STATUS_INFLIGHT.pop(key, None)
        inflight.set()
    return text


def invalidate_status_cache(
    git_dir: Path | None = None,
    work_tree: Path | None = None,
) -> None:
    """Drop cached porcelain rows (call after a successful commit)."""
    with _STATUS_LOCK:
        if git_dir is None and work_tree is None:
            _STATUS_CACHE.clear()
            return
        gd = str(Path(git_dir).resolve()) if git_dir is not None else None
        wt = str(Path(work_tree).resolve()) if work_tree is not None else None
        for key in list(_STATUS_CACHE):
            if gd is not None and key[0] != gd:
                continue
            if wt is not None and key[1] != wt:
                continue
            _STATUS_CACHE.pop(key, None)


def _is_ref_race(exc: GitError) -> bool:
    """True when a commit failed only because a concurrent writer held/advanced
    the ref or index — serialization doing its job, safe to retry. The first two
    are git's own errors; the third is the pre-commit guard's stale-base reject."""
    text = str(exc)
    return "cannot lock ref" in text or "index.lock" in text or "ledger HEAD advanced" in text


def git_available() -> bool:
    return shutil.which("git") is not None


def user_repo_git_dir(root: Path) -> Path | None:
    """Return ``<root>/.git`` when the workspace has a normal user repository."""
    candidate = Path(root).resolve() / ".git"
    # Plain dir or gitfile (worktree / submodule pointer).
    if candidate.is_dir() or candidate.is_file():
        return candidate
    return None


def commit_user_repo_paths(
    root: Path,
    message: str,
    paths: Sequence[str],
) -> str | None:
    """Stage and commit ``paths`` into the user's root ``.git`` (GitHub publish repo).

    Returns the new HEAD SHA, or ``None`` when nothing changed / no user repo.
    Does not touch the out-of-tree Horizon ledger. Used by static dashboard
    export so Pages artifacts land on the repository humans push, not only on
    the agent ledger.
    """
    root = Path(root).resolve()
    if user_repo_git_dir(root) is None:
        return None
    rels = [p for p in paths if p]
    if not rels:
        return None
    _run(["add", "--", *rels], cwd=root, check=True)
    staged = _run(
        ["diff", "--cached", "--name-only", "--", *rels],
        cwd=root,
        check=False,
    )
    if not staged.strip():
        return None
    _run(["commit", "-m", message, "--", *rels], cwd=root, check=True)
    return _run(["rev-parse", "HEAD"], cwd=root, check=True).strip() or None


def _run(
    args: Sequence[str],
    *,
    git_dir: Path | None = None,
    work_tree: Path | None = None,
    cwd: Path | None = None,
    check: bool = True,
    index_file: Path | None = None,
    extra_env: dict[str, str] | None = None,
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
        env=_git_env(index_file, extra_env),
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
    # Site / static export build trees (regenerable, often tens of thousands of files)
    "_site/", "site/dist/", "**/_site/",
    # Entire hgraph trees are regenerable (`horizon graph sync`) or optional agent
    # scratch on disk. The durable graph for dashboards is
    # `.archon-horizon/blueprints/<project>.json`; publish snapshots bake that in.
    # Users who want hgraph history put it in their own root `.git`.
    "**/hgraph/",
    "# Volatile session scratch — never history (I-1913)",
    "*.lock", "*.tmp", "**/*.lock", "**/*.tmp",
    "*.archon_tmp",
    # One-off probe / phase-audit snapshots left next to projects
    "**/.phase*/", "**/.tmp-*/", "**/.tmp/",
    "# OS / editor / local AI tooling",
    ".DS_Store", ".idea/", ".vscode/", ".claude/", ".codex/",
    "# Secrets — never commit credentials",
    ".env", ".env.*", "*.pem", "*.key", "id_rsa", "id_ed25519",
    "*.p12", "*.pfx", ".netrc", "*.secret", "secrets.yaml", "secrets.yml",
)

# Workspace-ledger-only excludes. The ledger is the *agent source journal*
# (Lean, blueprints, config.yaml) — not the live Horizon control plane.
# Roadmap/inbox/tasks/runs stay on disk for the dashboard and agents; the user
# root `.git` (or `horizon dashboard --static`) is how humans publish them.
# The ``bin/`` dir holds the auto-installed ``hgit`` wrapper.
_WORKSPACE_EXCLUDES = (
    # Whole state tree — never agent proof history.
    ".archon-horizon/",
)

# A pre-commit guard installed into every out-of-tree git. Two protections:
#
# 1. Secrets: an accidental credential (in a transcript, config, or dropped
#    file) is REDACTED to XXXX in the staged content and warned about — it never
#    blocks the commit. High-confidence, case-sensitive, key-prefixed formats
#    only, so ordinary content (long camelCase identifiers, lowercase base64) is
#    not touched. The redaction is best-effort and fails open: any error just
#    warns and lets the commit through, so a guard bug can never break commits.
#    Rotate any real credential regardless. ARCHON_HORIZON_ALLOW_SECRETS=1 skips
#    the scan entirely.
#
# 2. Silent clobbers on the shared ledger: a commit whose index was seeded
#    from a STALE HEAD (a concurrent session committed since the read-tree)
#    produces a tree that simply lacks the other session's new files — git
#    commits it without any error and the files are deleted. Likewise a plain
#    commit through a polluted shared index sweeps in thousands of staged
#    deletions. Both present the same way at commit time: staged deletions the
#    committer never asked for. The guard:
#      - if ARCHON_COMMIT_BASE is set (Archon's own integration commits set it
#        to the sha the private index was seeded from), require it to still be
#        HEAD — a cheap compare-and-swap; the caller re-seeds and retries;
#      - otherwise (an agent's plain git) reject any commit that stages
#        deletions, unless ARCHON_HORIZON_ALLOW_DELETIONS=1 says they are
#        intentional. New/modified files are always fine.
_SECRET_HOOK = r"""#!/bin/sh
# Auto-installed by Archon Horizon. Redacts obvious secrets (never blocks) and
# blocks only silent clobbers of the shared ledger.
# High-confidence, key-prefixed, CASE-SENSITIVE patterns only (grep -E, no -i),
# so long camelCase identifiers and lowercase base64 never match.
ARCHON_SECRET_PAT='ghp_[0-9A-Za-z]{30,}|gho_[0-9A-Za-z]{30,}|github_pat_[0-9A-Za-z_]{30,}|sk-ant-[0-9A-Za-z_-]{20,}|sk-[0-9A-Za-z]{20,}|xox[baprs]-[0-9A-Za-z-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY-----'
if [ "$ARCHON_HORIZON_ALLOW_SECRETS" != "1" ]; then
  # Redact each staged addition/modification in place, then re-stage it. Every
  # step is guarded so a failure only warns — the commit is never blocked here.
  archon_redacted=""
  for f in $(git diff --cached --name-only --diff-filter=AM 2>/dev/null); do
    [ -f "$f" ] || continue
    LC_ALL=C grep -Eq "$ARCHON_SECRET_PAT" "$f" 2>/dev/null || continue
    if LC_ALL=C sed -E "s/($ARCHON_SECRET_PAT)/XXXX/g" "$f" > "$f.archon_tmp" 2>/dev/null && mv "$f.archon_tmp" "$f" 2>/dev/null; then
      # -f: path may already be staged despite info/exclude (legacy force-add).
      git add -f -- "$f" 2>/dev/null && archon_redacted="$archon_redacted $f"
    else
      rm -f "$f.archon_tmp" 2>/dev/null
    fi
  done
  if [ -n "$archon_redacted" ]; then
    echo "Archon Horizon: redacted possible secret(s) to XXXX in:$archon_redacted" >&2
    echo "The commit proceeds with the redacted content — ROTATE any real credential." >&2
  fi
fi

head=$(git rev-parse --verify --quiet HEAD) || head=""
[ -n "$head" ] || exit 0   # first commit: nothing to clobber

# Optional path allowlist file (one path/prefix per line in
# ARCHON_COMMIT_PATHS_FILE). When set, the staged set must be a subset of those
# paths/prefixes — concurrent writers' files that leaked into a broad add are
# rejected (I-0409).
if [ -n "$ARCHON_COMMIT_PATHS_FILE" ] && [ -f "$ARCHON_COMMIT_PATHS_FILE" ]; then
  bad=$(git diff --cached --name-only 2>/dev/null | while IFS= read -r p; do
    [ -n "$p" ] || continue
    ok=0
    while IFS= read -r allow; do
      [ -n "$allow" ] || continue
      case "$p" in
        "$allow"|"$allow"/*) ok=1; break ;;
      esac
    done < "$ARCHON_COMMIT_PATHS_FILE"
    [ "$ok" = "1" ] || printf '%s\n' "$p"
  done)
  if [ -n "$bad" ]; then
    n=$(printf '%s\n' "$bad" | sed '/^$/d' | wc -l | tr -d ' ')
    echo "Archon Horizon: this commit stages $n path(s) outside the explicit add set, e.g.:" >&2
    printf '%s\n' "$bad" | sed '/^$/d' | head -8 >&2
    echo "Re-seed from HEAD and add ONLY the paths you intend to commit." >&2
    exit 1
  fi
fi

if [ -n "$ARCHON_COMMIT_BASE" ]; then
  if [ "$ARCHON_COMMIT_BASE" != "$head" ]; then
    echo "Archon Horizon: ledger HEAD advanced since this index was seeded" >&2
    echo "(base $ARCHON_COMMIT_BASE, HEAD $head). Committing now would silently" >&2
    echo "revert the concurrent session's files. Re-seed and retry." >&2
    exit 1
  fi
  exit 0
fi

[ "$ARCHON_HORIZON_ALLOW_DELETIONS" = "1" ] && exit 0
dels=$(git diff --cached --name-only --diff-filter=D 2>/dev/null)
if [ -n "$dels" ]; then
  n=$(printf '%s\n' "$dels" | wc -l | tr -d ' ')
  echo "Archon Horizon: this commit would DELETE $n tracked file(s) you did not change, e.g.:" >&2
  printf '%s\n' "$dels" | head -5 >&2
  echo "This almost always means the index was seeded from a stale HEAD (a concurrent" >&2
  echo "session committed since your read-tree) or you are committing through the" >&2
  echo "polluted shared index — committing would silently destroy that work." >&2
  echo "Fix: re-seed against the current HEAD and re-add ONLY your files:" >&2
  echo "  git read-tree HEAD && git add -- <your files> && git commit ..." >&2
  echo "If the deletions ARE intentional, set ARCHON_HORIZON_ALLOW_DELETIONS=1 for this commit." >&2
  exit 1
fi
exit 0
"""


# A prepare-commit-msg hook that stamps run/session/task provenance as git
# trailers onto commits made from inside a Horizon session, so the dashboard can
# link a commit to its session/task WITHOUT a custom commit wrapper. The agent
# writes only a semantic message; provenance is added here. Idempotent: a no-op
# outside a session (no ARCHON_HORIZON_RUN) or when the message is already stamped
# (e.g. the Python integration path put the trailers in itself).
_PROVENANCE_HOOK = r"""#!/bin/sh
# Auto-installed by Archon Horizon. Adds Archon-* provenance trailers from the env.
msg="$1"
[ -n "$ARCHON_HORIZON_RUN" ] || exit 0
[ -n "$msg" ] || exit 0
grep -q '^Archon-Run:' "$msg" 2>/dev/null && exit 0
set -- --trailer "Archon-Run=$ARCHON_HORIZON_RUN"
[ -n "$ARCHON_HORIZON_AGENT_ROLE" ] && set -- "$@" --trailer "Archon-Role=$ARCHON_HORIZON_AGENT_ROLE"
[ -n "$ARCHON_HORIZON_SESSION" ] && set -- "$@" --trailer "Archon-Session=$ARCHON_HORIZON_SESSION"
[ -n "$ARCHON_HORIZON_TASK" ] && set -- "$@" --trailer "Archon-Task=$ARCHON_HORIZON_TASK"
[ -n "$ARCHON_HORIZON_PROJECTS" ] && set -- "$@" --trailer "Archon-Projects=$ARCHON_HORIZON_PROJECTS"
git interpret-trailers --in-place --trailer "Archon-Commit=agent" "$@" "$msg" 2>/dev/null || exit 0
exit 0
"""

# A thin plain-git passthrough to the workspace ledger, installed under
# ``<state>/bin/hgit`` so an agent commits with normal git semantics WITHOUT
# exporting GIT_DIR/GIT_WORK_TREE globally — which would redirect ``lake`` and the
# project's own git too. Reads the ledger paths from the session env.
_LEDGER_GIT_WRAPPER = """#!/bin/sh
# Auto-installed by Archon Horizon. `git` against the workspace ledger.
exec git --git-dir="$HORIZON_LEDGER_GIT_DIR" --work-tree="$HORIZON_LEDGER_WORK_TREE" "$@"
"""


def install_ledger_git_wrapper(state_dir: Path) -> Path | None:
    """Write the ``hgit`` ledger-git passthrough into ``<state>/bin`` and return
    its path. Idempotent. Returns ``None`` on failure — the explicit
    ``git --git-dir=… --work-tree=…`` form documented in the skill still works."""
    try:
        bin_dir = Path(state_dir) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        path = bin_dir / "hgit"
        path.write_text(_LEDGER_GIT_WRAPPER, "utf-8")
        path.chmod(0o755)
        return path
    except OSError:
        return None


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
    prov = hooks / "prepare-commit-msg"
    prov.write_text(_PROVENANCE_HOOK, "utf-8")
    prov.chmod(0o755)


def _prune_ignored_from_index(git_dir: Path, work_tree: Path, index_file: Path | None = None) -> None:
    """Drop already-tracked but now-ignored paths from the index (e.g. ``.lake``
    committed before the excludes existed), so the next commit records their
    removal. Touches only the index (``--cached``); the working tree is left
    intact. Self-heals a workspace that was bloated by the old force-add.

    ``index_file`` targets a private index — commits stage there, so the pruning
    has to happen in the index the commit is actually built from."""
    listed = _run(
        ["ls-files", "-z", "-ci", "--exclude-standard"],
        git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False, index_file=index_file,
    )
    paths = [p for p in listed.split("\0") if p]
    if not paths:
        return
    # Batch to stay well under ARG_MAX on large trees (thousands of .lake files).
    for start in range(0, len(paths), 500):
        _run(
            ["rm", "--cached", "-q", "--", *paths[start:start + 500]],
            git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False, index_file=index_file,
        )


def _is_volatile_ledger_path(path: str) -> bool:
    """True for paths that must never enter the agent ledger."""
    name = path.rsplit("/", 1)[-1]
    if name in {"process.json", ".create.lock"}:
        return True
    if name.endswith(".lock") or name.endswith(".tmp") or name.endswith(".archon_tmp"):
        return True
    # Entire Horizon state tree (dashboard/inbox/runs live on disk only).
    if path == ".archon-horizon" or path.startswith(".archon-horizon/"):
        return True
    # Generated or local-only semantic graph files.
    if "/hgraph/" in f"/{path}/" or path.endswith("/hgraph") or path == "hgraph":
        return True
    return False


def _is_non_ledger_path(path: str) -> bool:
    """Public alias: path should not be recorded in the agent ledger."""
    return _is_volatile_ledger_path(path)


def _unstage_volatile_paths(
    git_dir: Path, work_tree: Path, index_file: Path | None = None,
) -> None:
    """Drop staged lock/tmp/process-marker paths without undoing force-adds."""
    listed = _run(
        ["diff", "--cached", "--name-only", "-z"],
        git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False, index_file=index_file,
    )
    paths = [p for p in listed.split("\0") if p and _is_volatile_ledger_path(p)]
    for start in range(0, len(paths), 500):
        _run(
            ["rm", "--cached", "-q", "--ignore-unmatch", "--", *paths[start:start + 500]],
            git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False, index_file=index_file,
        )


def _unstage_gitlinks(git_dir: Path, work_tree: Path, index_file: Path, paths: Sequence[str] | None = None) -> None:
    """Drop submodule gitlink entries (mode 160000) that are plain directories on disk.

    A commit's index is seeded from HEAD, so a gitlink committed before the nested
    ``.git`` was neutralized comes back with it. Dropping it lets the following
    ``add`` re-track the project's actual files, converting the gitlink to a real
    tree. A path that still owns a live ``.git`` is left alone."""
    args = ["ls-files", "-s"]
    if paths:
        args += ["--", *paths]
    listing = _run(
        args, git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False, index_file=index_file
    )
    stale = [
        path
        for line in listing.splitlines()
        if line.startswith("160000") and "\t" in line
        for path in (line.split("\t", 1)[1],)
        if not (work_tree / path / ".git").exists()
    ]
    for start in range(0, len(stale), 500):
        _run(
            ["rm", "--cached", "-q", "--ignore-unmatch", "--", *stale[start:start + 500]],
            git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False, index_file=index_file,
        )


def _sync_index_to_head(git_dir: Path, work_tree: Path) -> None:
    """Point the repo's **shared** index at HEAD, the way a checkout would.

    Nothing ever checks this out-of-tree repo out, so git never populates its
    shared index on its own. Left alone it is either *empty* — in which case a
    plain ``git add F && git commit`` records a tree containing only ``F`` and
    **deletes every other file** — or it is full of some previous flow's leftover
    staging, which a plain ``git commit`` would sweep in wholesale and which makes
    the ``pre-commit`` secret guard scan (and reject on) unrelated files.

    Archon's own commits stage in a private index, so nothing here fights them.
    Keeping the shared index a faithful mirror of HEAD is what makes the ordinary
    ``git add`` / ``git commit`` an agent runs behave the way it does in any
    normal repository."""
    head = _run(
        ["rev-parse", "--verify", "--quiet", "HEAD"],
        git_dir=git_dir, work_tree=work_tree, cwd=work_tree, check=False,
    )
    _run(
        ["read-tree", head] if head else ["read-tree", "--empty"],
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


def _git_env(index_file: Path | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Archon Horizon")
    env.setdefault("GIT_AUTHOR_EMAIL", "archon-horizon@local")
    env.setdefault("GIT_COMMITTER_NAME", "Archon Horizon")
    env.setdefault("GIT_COMMITTER_EMAIL", "archon-horizon@local")
    if index_file is not None:
        # Stage into a PRIVATE index instead of the repo's shared one. See
        # WorkspaceGit.commit for why this matters.
        env["GIT_INDEX_FILE"] = str(index_file)
    if extra:
        env.update(extra)
    return env


class WorkspaceGit:
    """The workspace's own repository (the manifest root).

    Kept **out-of-tree** at ``.archon-horizon/vcs/workspace.git`` and driven via
    ``--git-dir`` / ``--work-tree``, so it never creates a root ``.git`` that
    would collide with — or commit into — the user's own repository if they run
    Archon Horizon inside one. The work tree is the workspace root.
    """

    def __init__(self, root: Path, git_dir: Path | None = None) -> None:
        self.root = root.resolve()
        self.git_dir = git_dir.resolve() if git_dir else (
            self.root / ".archon-horizon" / "vcs" / "workspace.git"
        )

    def _run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        index_file: Path | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        # cwd=root so relative pathspecs resolve against the work tree; the
        # explicit --git-dir means git never discovers a parent .git.
        return _run(
            args,
            git_dir=self.git_dir,
            work_tree=self.root,
            cwd=self.root,
            check=check,
            index_file=index_file,
            extra_env=extra_env,
        )

    def is_repo(self) -> bool:
        return self.git_dir.exists()

    def init(self) -> None:
        if not self.is_repo():
            self.git_dir.parent.mkdir(parents=True, exist_ok=True)
            _init_bare(self.git_dir)
        # Always refresh excludes + secret hook so existing workspaces self-heal,
        # then reset the shared index to HEAD — clearing any staging a pre-private-index
        # Horizon (or an interrupted commit) stranded there, which would otherwise be
        # swept into the next plain `git commit` and trip the secret guard.
        _ensure_repo_hygiene(self.git_dir, extra_excludes=_WORKSPACE_EXCLUDES)
        _sync_index_to_head(self.git_dir, self.root)

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
        from prose. Empty values are dropped.

        Staging happens in a **private index**, never the repo's shared one. The
        shared index is a single mutable resource that agents also commit against
        with plain ``git``; staging thousands of files into it made every other
        commit either sweep those files in (``git commit`` commits the whole
        index) or be rejected outright by the ``pre-commit`` secret guard, which
        scans everything staged. Worse, it was self-poisoning: a commit that
        failed or was interrupted left its staging behind, wedging the ledger for
        good. A private index keeps this atomic — nothing is left behind on
        failure, and a concurrent agent's index is untouched.
        """
        if trailers:
            message = message.rstrip() + "\n\n" + "".join(
                f"{key}: {value}\n" for key, value in trailers.items() if value
            )

        with tempfile.TemporaryDirectory(prefix="archon-index-") as tmp:
            index = Path(tmp) / "index"
            allow_file: Path | None = None
            if paths is not None:
                # Feed the pre-commit allowlist so a concurrent writer's files
                # that somehow land in the private index cannot ride along
                # (I-0409). One path/prefix per line; empty lines ignored.
                allow_file = Path(tmp) / "commit-paths"
                allow_file.write_text(
                    "".join(f"{p}\n" for p in paths if p),
                    "utf-8",
                )
            # Re-seed and retry if HEAD moves under us: the private index is built
            # from a snapshot of HEAD, so committing against a HEAD that has since
            # advanced would write a tree that silently reverts the other writer's
            # files. Comparing HEAD before/after staging turns that data loss into
            # a retry. Concurrent `horizon run` processes against one shared
            # ledger make this window real, so allow several attempts.
            attempts = 5
            for attempt in range(attempts):
                head = self.current_sha()
                self._run(["read-tree", head] if head else ["read-tree", "--empty"], index_file=index)
                # Drop now-ignored paths inherited from HEAD, so this commit records
                # their removal (the self-heal for trees bloated by the old force-add),
                # and likewise any gitlink HEAD still carries for a neutralized repo.
                _prune_ignored_from_index(self.git_dir, self.root, index)
                _unstage_gitlinks(self.git_dir, self.root, index, paths)

                if paths is None:
                    self._run(["add", "-A"], index_file=index)
                    # Broad add still respects info/exclude; strip anything that
                    # slipped through (state tree, hgraph, locks).
                    _unstage_volatile_paths(self.git_dir, self.root, index)
                else:
                    # ``config.yaml`` is force-added so a user root ``.gitignore``
                    # that ignores it cannot hide workspace config from the
                    # ledger. Project trees are added WITHOUT force so excludes
                    # apply (``.lake``, ``**/hgraph/``, ``.archon-horizon/``, …).
                    # Horizon state is never staged — it stays on disk only.
                    config_paths = [p for p in paths if p == "config.yaml"]
                    projects = [
                        p for p in paths
                        if p != "config.yaml" and not _is_volatile_ledger_path(p)
                    ]
                    if config_paths:
                        self._run(["add", "-f", "--", *config_paths], index_file=index)
                    if projects:
                        # Fully-ignored pathspecs (e.g. ``_site/``, a project
                        # that is only build artifacts) make ``git add`` exit
                        # non-zero with "paths are ignored". That is success
                        # for our floor excludes — treat it as "nothing to add"
                        # rather than aborting the integration commit.
                        self._run(
                            ["add", "-A", "--", *projects],
                            index_file=index,
                            check=False,
                        )
                    _unstage_volatile_paths(self.git_dir, self.root, index)
                    # After a broad ``add -A`` under a pathspec, drop anything
                    # staged outside the explicit allowlist (belt-and-suspenders
                    # with the pre-commit guard — keeps the private index clean
                    # even if a future git quirk widens the add).
                    self._drop_paths_outside_allowlist(index, paths)

                # Only the index column (porcelain's first char) counts: a bare
                # `status` is non-empty for untracked/dirty files we did not stage,
                # which would push us into a `commit` that has nothing to record.
                staged = any(
                    line[:1] not in (" ", "?", "")
                    for line in self._run(
                        ["status", "--porcelain", "--untracked-files=no"],
                        index_file=index,
                    ).splitlines()
                )
                if not staged and not allow_empty:
                    return None

                if self.current_sha() != head:
                    continue  # HEAD advanced while we staged — rebuild on the new one.

                args = ["commit", *_author_args(author)]
                if allow_empty:
                    args.append("--allow-empty")
                try:
                    # ARCHON_COMMIT_BASE lets the pre-commit guard verify the
                    # index is still based on the current HEAD at hook time —
                    # closing the window between the staleness check above and
                    # the commit, where a concurrent writer's files would be
                    # silently reverted by our (now stale) tree.
                    # ARCHON_COMMIT_PATHS_FILE is the staged-path allowlist.
                    extra: dict[str, str] = {}
                    if head:
                        extra["ARCHON_COMMIT_BASE"] = head
                    if allow_file is not None:
                        extra["ARCHON_COMMIT_PATHS_FILE"] = str(allow_file)
                    self._run(
                        [*args, "-m", message],
                        index_file=index,
                        extra_env=extra or None,
                    )
                except GitError as exc:
                    # A writer we didn't see (an agent's plain `git`, another
                    # run's boundary commit) beat us to the ref between the HEAD
                    # check above and the commit: git's own compare-and-swap
                    # rejects with "cannot lock ref 'HEAD': is at X but expected
                    # Y" (or an index.lock collision). Transient — re-seed from
                    # the new HEAD and try again.
                    if attempt + 1 < attempts and _is_ref_race(exc):
                        time.sleep(0.1 * (attempt + 1) + random.uniform(0.0, 0.2))
                        continue
                    raise
                # HEAD moved; keep the shared index tracking it so the plain `git`
                # an agent runs still sees a normal, HEAD-mirroring index.
                _sync_index_to_head(self.git_dir, self.root)
                invalidate_status_cache(self.git_dir, self.root)
                return self.current_sha()
            # Every attempt lost the race. Surface it — falling through to None
            # would report "nothing changed" for a commit that was never made.
            raise GitError(
                f"workspace commit lost the ledger race {attempts} times "
                "(concurrent writers keep advancing HEAD); try again"
            )

        raise GitError("ledger HEAD kept moving while staging; commit abandoned after 3 attempts")

    def _drop_paths_outside_allowlist(
        self, index_file: Path, allow: Sequence[str],
    ) -> None:
        """Unstage anything in the private index that is not under ``allow``.

        ``git add -A -- <dir>`` is pathspec-scoped, but a force-add of a broad
        Horizon state tree can still pick up concurrent writers' files when the
        allowlist is a parent directory. The pre-commit guard is the last line
        of defense; this keeps the staged set clean before we get there.
        """
        allowed = tuple(p for p in allow if p)
        if not allowed:
            return
        listing = self._run(
            ["diff", "--cached", "--name-only"],
            index_file=index_file,
            check=False,
        )
        outsiders = [
            path for path in listing.splitlines()
            if path and not any(
                path == prefix or path.startswith(prefix + "/")
                for prefix in allowed
            )
        ]
        for start in range(0, len(outsiders), 500):
            batch = outsiders[start:start + 500]
            # ``rm --cached`` drops the path from the private index regardless
            # of whether HEAD knows it; safer than ``reset`` against an empty
            # private index mid-seed.
            self._run(
                ["rm", "--cached", "-q", "--ignore-unmatch", "--", *batch],
                index_file=index_file,
                check=False,
            )

    def current_sha(self) -> str | None:
        # --verify --quiet: prints nothing (instead of echoing "HEAD") and exits
        # non-zero when the branch is unborn, so an empty repo reads as None.
        sha = self._run(["rev-parse", "--verify", "--quiet", "HEAD"], check=False)
        return sha or None

    def log(self, *, paths: Sequence[str] = (), limit: int = 100) -> list[dict[str, str]]:
        """Recent commits, newest-first, optionally scoped to workspace paths."""
        if not self.is_repo():
            return []
        args = [
            "log",
            "-n",
            str(max(1, limit)),
            "--pretty=format:%H%x00%h%x00%s%x00%aI",
        ]
        clean_paths = [p for p in paths if p]
        if clean_paths:
            args += ["--", *clean_paths]
        out = self._run(args, check=False)
        rows: list[dict[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\0")
            if len(parts) >= 4:
                rows.append({
                    "sha": parts[0],
                    "short_sha": parts[1],
                    "subject": parts[2],
                    "date": parts[3],
                })
        return rows

    def files_at(self, sha: str, paths: Sequence[str] = ()) -> tuple[str, ...]:
        """Workspace-relative files present at ``sha``, optionally path-scoped."""
        if not self.is_repo():
            return ()
        args = ["ls-tree", "-r", "--name-only", sha]
        clean_paths = [p for p in paths if p]
        if clean_paths:
            args += ["--", *clean_paths]
        out = self._run(args, check=False)
        return tuple(line.strip() for line in out.splitlines() if line.strip())

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

    def list_non_ledger_tracked_paths(self) -> tuple[str, ...]:
        """Tracked paths that current policy says must not live in the ledger.

        Used by ``horizon ledger prune`` to drop historical ``.archon-horizon/``
        and ``**/hgraph/`` trees from the index without deleting working-tree
        files (dashboard/inbox/runs stay on disk).
        """
        if not self.is_repo():
            return ()
        # Refresh excludes first so --exclude-standard matches current policy.
        _ensure_repo_hygiene(self.git_dir, extra_excludes=_WORKSPACE_EXCLUDES)
        listed = self._run(
            ["ls-files", "-z", "-ci", "--exclude-standard"],
            check=False,
        )
        paths = [p for p in listed.split("\0") if p]
        # Also catch anything that slipped past exclude patterns but is still
        # non-ledger by path shape (e.g. odd force-adds of state).
        all_tracked = self._run(["ls-files", "-z"], check=False)
        extra = [
            p for p in all_tracked.split("\0")
            if p and _is_volatile_ledger_path(p) and p not in paths
        ]
        return tuple(dict.fromkeys([*paths, *extra]))

    def prune_non_ledger_paths(
        self,
        *,
        message: str = "ledger: drop Horizon state and generated hgraph from agent journal",
        dry_run: bool = False,
        gc: bool = False,
    ) -> dict[str, object]:
        """Remove non-ledger paths from HEAD (index-only), optionally ``git gc``.

        Working-tree files are left intact. Returns a summary dict suitable for
        CLI JSON output. Safe under concurrent agents: uses the usual private
        index + deletion allow env for this intentional prune commit.
        """
        self.init()
        victims = self.list_non_ledger_tracked_paths()
        summary: dict[str, object] = {
            "paths": len(victims),
            "sample": list(victims[:20]),
            "sha": None,
            "dry_run": dry_run,
            "gc": False,
        }
        if not victims:
            return summary
        if dry_run:
            return summary
        # Large trees + concurrent agent commits: re-seed and retry if HEAD moves
        # while we batch ``rm --cached`` (same CAS idea as WorkspaceGit.commit).
        last_err: str | None = None
        for attempt in range(5):
            victims = self.list_non_ledger_tracked_paths()
            summary["paths"] = len(victims)
            summary["sample"] = list(victims[:20])
            if not victims:
                return summary
            with tempfile.TemporaryDirectory(prefix="archon-prune-") as tmp:
                index = Path(tmp) / "index"
                head = self.current_sha()
                self._run(
                    ["read-tree", head] if head else ["read-tree", "--empty"],
                    index_file=index,
                )
                for start in range(0, len(victims), 500):
                    batch = list(victims[start:start + 500])
                    self._run(
                        ["rm", "--cached", "-q", "--ignore-unmatch", "--", *batch],
                        index_file=index,
                    )
                # Bail early if HEAD already moved during the long rm pass.
                if head and self.current_sha() != head:
                    last_err = "ledger HEAD advanced during prune staging"
                    continue
                extra: dict[str, str] = {"ARCHON_HORIZON_ALLOW_DELETIONS": "1"}
                if head:
                    extra["ARCHON_COMMIT_BASE"] = head
                try:
                    self._run(
                        ["commit", "-m", message],
                        index_file=index,
                        extra_env=extra,
                    )
                    last_err = None
                    break
                except GitError as exc:
                    text = str(exc)
                    if "ledger HEAD advanced" in text or "cannot lock ref" in text:
                        last_err = text
                        continue
                    raise
        else:
            raise GitError(
                f"ledger prune lost the HEAD race {5} times"
                + (f": {last_err}" if last_err else "")
            )
        sha = self.current_sha()
        summary["sha"] = sha
        _sync_index_to_head(self.git_dir, self.root)
        invalidate_status_cache(self.git_dir, self.root)
        if gc:
            # Drop unreachable blobs from historical state/hgraph commits.
            self._run(["gc", "--prune=now"], check=False)
            summary["gc"] = True
        return summary

    def changed_files(
        self,
        *,
        untracked: str = "no",
    ) -> tuple[str, ...]:
        """Workspace-relative paths with uncommitted changes (porcelain).

        Defaults to ``untracked=no``: listing every untracked file under a large
        worktree (references dumps, ``_site/``, ``.lake`` leftovers) is both
        expensive and the root of multi-minute concurrent ``git status`` storms
        on mature workspaces. Callers that truly need untracked files can pass
        ``untracked="normal"`` or ``"all"``.
        """
        if not self.is_repo():
            return ()
        status = status_porcelain(
            self.git_dir,
            self.root,
            untracked=untracked,
        )
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

    def commit_shas_between(self, base: str | None, head: str | None) -> tuple[str, ...]:
        """Commits reachable from ``head`` but not ``base``, oldest-first."""
        if not self.is_repo() or not head or head == base:
            return ()
        revision = f"{base}..{head}" if base else head
        out = self._run(["rev-list", "--reverse", revision], check=False)
        return tuple(line.strip() for line in out.splitlines() if line.strip())

    def commits_detailed_by_refs(self, refs: Sequence[str]) -> list[dict[str, str]]:
        """Resolve commit refs and return provenance rows in the given order.

        Invalid refs are ignored. This backs recovery for sessions that recorded
        the exact commit SHA but predate automatic Archon trailers.
        """
        rows: list[dict[str, str]] = []
        seen: set[str] = set()
        for ref in refs:
            if not ref:
                continue
            sha = self._run(
                ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                check=False,
            )
            if not sha or sha in seen:
                continue
            seen.add(sha)
            out = self._run(
                [
                    "show", "-s",
                    "--format=%H%x1f%s%x1f%cI%x1f"
                    "%(trailers:key=Archon-Run,valueonly,separator=%x1e)%x1f"
                    "%(trailers:key=Archon-Session,valueonly,separator=%x1e)%x1f"
                    "%(trailers:key=Archon-Role,valueonly,separator=%x1e)%x1f"
                    "%(trailers:key=Archon-Commit,valueonly,separator=%x1e)%x1f"
                    "%(trailers:key=Summary,valueonly,separator=%x1e)",
                    sha,
                ],
                check=False,
            )
            parts = out.split("\x1f")
            if len(parts) > 8:
                continue
            parts.extend([""] * (8 - len(parts)))
            (
                full_sha,
                subject,
                date,
                run_trailer,
                session_trailer,
                role_trailer,
                kind_trailer,
                summary_trailer,
            ) = parts
            runs = [v.strip() for v in run_trailer.split("\x1e") if v.strip()]
            sessions = [v.strip() for v in session_trailer.split("\x1e") if v.strip()]
            roles = [v.strip().lower() for v in role_trailer.split("\x1e") if v.strip()]
            kinds = [v.strip().lower() for v in kind_trailer.split("\x1e") if v.strip()]
            summaries = [v.strip() for v in summary_trailer.split("\x1e") if v.strip()]
            rows.append({
                "sha": full_sha,
                "subject": subject,
                "summary": "\n\n".join(summaries),
                "date": date,
                "run": runs[-1] if runs else "",
                "session": sessions[-1] if sessions else "",
                "role": roles[-1] if roles else "",
                "kind": kinds[-1] if kinds else "agent",
            })
        return rows

    def _summary_for_commit(self, sha: str) -> str:
        """Read every Summary trailer for one commit.

        ``git log --grep`` currently collapses repeated trailers to the first
        value, while ``git show`` preserves them. Resolve this small field per
        matched commit so several explanations remain visible in the dashboard.
        """
        raw = self._run(
            [
                "show",
                "-s",
                "--format=%(trailers:key=Summary,valueonly,separator=%x1e)",
                sha,
            ],
            check=False,
        )
        return "\n\n".join(v.strip() for v in raw.split("\x1e") if v.strip())

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
             "--format=%H%x1f%s%x1f%cI%x1f"
             "%(trailers:key=Archon-Run,valueonly,separator=%x1e)%x1f"
             "%(trailers:key=Archon-Session,valueonly,separator=%x1e)%x1f"
             "%(trailers:key=Archon-Role,valueonly,separator=%x1e)%x1f"
             "%(trailers:key=Archon-Commit,valueonly,separator=%x1e)"],
            check=False,
        ) or ""
        matched: list[dict[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) != 7:
                continue
            (
                sha,
                subject,
                date,
                run_trailer,
                session_trailer,
                role_trailer,
                kind_trailer,
            ) = parts
            runs = [v.strip() for v in run_trailer.split("\x1e") if v.strip()]
            sessions = [v.strip() for v in session_trailer.split("\x1e") if v.strip()]
            if run_id in runs and session in sessions:
                kinds = [v.strip().lower() for v in kind_trailer.split("\x1e") if v.strip()]
                kind = kinds[-1] if kinds else ("integration" if subject.startswith("workspace[") and ": integrate " in subject else "agent")
                roles = [v.strip().lower() for v in role_trailer.split("\x1e") if v.strip()]
                matched.append({
                    "sha": sha,
                    "subject": subject,
                    "summary": self._summary_for_commit(sha),
                    "date": date,
                    "role": roles[-1] if roles else "",
                    "kind": kind,
                })
        matched.reverse()  # oldest-first
        return matched

    def numstat(self, base: str | None, sha: str, paths: Sequence[str] = ()) -> list[tuple[int, int, str]]:
        """``(added, deleted, path)`` per changed file between two commits.
        A ``-`` count (binary) reads as 0."""
        if not self.is_repo():
            return []
        args = ["diff", "--numstat", base or self._EMPTY_TREE, sha]
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
