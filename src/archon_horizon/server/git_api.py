"""Git history for a project directory.

A project may be its own git repo, or just a subdirectory of the workspace
repo (the common case here). Either way we resolve an enclosing ``.git`` and,
when the project is a subdirectory, scope the log/diff to that subtree with a
pathspec so the history is meaningful.
"""

import subprocess
from pathlib import Path


def _resolve_repo(repo_path: Path) -> tuple[Path, str | None] | None:
    """Return ``(git_root, pathspec)`` for ``repo_path``.

    ``pathspec`` is None when ``repo_path`` is itself a repo root, otherwise the
    project's path relative to the enclosing repo (to scope history to it).
    """
    repo_path = repo_path.resolve()
    if (repo_path / ".git").exists():
        return repo_path, None
    for parent in repo_path.parents:
        if (parent / ".git").exists():
            return parent, repo_path.relative_to(parent).as_posix()
    return None


def get_git_log(repo_path: Path):
    resolved = _resolve_repo(repo_path)
    if resolved is None:
        return {"commits": []}
    git_root, pathspec = resolved
    cmd = ["git", "-C", str(git_root), "log", "-n", "100",
           "--pretty=format:%H%x00%h%x00%s%x00%aI%x00%P%x00%D"]
    if pathspec:
        cmd += ["--", pathspec]
    res = subprocess.run(cmd, capture_output=True, text=True)
    commits = []
    if res.returncode == 0 and res.stdout:
        for line in res.stdout.split("\n"):
            parts = line.split("\0")
            if len(parts) >= 6:
                commits.append({
                    "sha": parts[0],
                    "shortSha": parts[1],
                    "subject": parts[2],
                    "date": parts[3],
                    "parents": parts[4].split() if parts[4] else [],
                    "refs": [r.strip() for r in parts[5].split(",")] if parts[5] else [],
                })
    return {"commits": commits}


def get_git_diff(repo_path: Path, commit: str):
    resolved = _resolve_repo(repo_path)
    if resolved is None:
        return ""
    git_root, pathspec = resolved
    cmd = ["git", "-C", str(git_root), "show", commit, "--format="]
    if pathspec:
        cmd += ["--", pathspec]
    res = subprocess.run(cmd, capture_output=True, text=True)
    return res.stdout if res.returncode == 0 else ""
