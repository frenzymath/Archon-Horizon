"""Deterministic per-session change summaries for the Logs view.

Each session is one commit in the single workspace ledger (see
:mod:`archon_horizon.vcs.git`). We diff that commit against its parent — scoped
to the projects the session could write — and, per file, compute the ``sorry``
delta and the before/after LOC (total and code-only). Nothing here is
AI-generated: the numbers come straight from git plus the same sorry/LOC scanner
the file list uses, so the Logs view shows what actually changed rather than
another prose report.

Files are classified so the UI can show LEAN and BLUEPRINT (`.tex`) views
separately and keep LOC/sorry stats to the files that matter — the bundled
shared-state files that ride the same commit (events log, roadmap, config, …)
are counted but never mixed into the Lean numbers.

Degrades gracefully: if git is unavailable or a commit is missing (e.g. a cloned
workspace that did not carry the out-of-tree repo), callers fall back to the
file list recorded on the integration event, so the view still shows *which*
files changed even without a line-level diff.
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from archon_horizon.server.source_api import _scan, file_stats
from archon_horizon.vcs.git import WorkspaceGit, git_available

# Cap a single file's inline diff so one giant generated file can't bloat the
# payload; the UI shows a truncation note and the raw churn still reflects it.
_MAX_FILE_DIFF_CHARS = 60_000

_LEAN_DECL_RE = re.compile(
    r"^\s*(?:private\s+|protected\s+|noncomputable\s+|unsafe\s+|partial\s+)*"
    r"(lemma|theorem|example|def|instance|class|structure|inductive|abbrev)\s+([^\s:(\[{]+)"
)
_BLUEPRINT_ENV_RE = re.compile(
    r"\\begin\s*\{"
    r"(theorem|lemma|proposition|corollary|definition|conjecture|remark|example|notation|convention)"
    r"\}"
)


def file_category(path: str) -> str:
    if path.endswith(".lean"):
        return "lean"
    if path.endswith(".tex"):
        return "blueprint"
    return "other"


def _stats(text: str | None) -> dict[str, int]:
    """LOC/sorry stats for a blob, or zeros when the file did not exist."""
    if not text:
        return {"loc": 0, "loc_code": 0, "sorries": 0}
    return file_stats(text)


def _lean_decl_counts(text: str | None) -> dict[str, int]:
    if not text:
        return {}
    code, _ = _scan(text)
    counts: dict[str, int] = {}
    for line in code.splitlines():
        match = _LEAN_DECL_RE.match(line)
        if match:
            kind = match.group(1)
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _strip_tex_comments(text: str) -> str:
    out: list[str] = []
    for line in text.splitlines():
        buf: list[str] = []
        escaped = False
        for ch in line:
            if ch == "%" and not escaped:
                break
            buf.append(ch)
            escaped = (ch == "\\") and not escaped
            if ch != "\\":
                escaped = False
        out.append("".join(buf))
    return "\n".join(out)


def _blueprint_decl_counts(text: str | None) -> dict[str, int]:
    if not text:
        return {}
    counts: dict[str, int] = {}
    for match in _BLUEPRINT_ENV_RE.finditer(_strip_tex_comments(text)):
        kind = match.group(1)
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _decl_counts(category: str, text: str | None) -> dict[str, int]:
    if category == "lean":
        return _lean_decl_counts(text)
    if category == "blueprint":
        return _blueprint_decl_counts(text)
    return {}


def _count_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    keys = sorted(set(before) | set(after))
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys if after.get(k, 0) != before.get(k, 0)}


def _file_row(git: WorkspaceGit, base: str | None, sha: str | None, add: int, dele: int, path: str) -> dict[str, Any]:
    category = file_category(path)
    row: dict[str, Any] = {"path": path, "category": category, "add": add, "del": dele}
    if category == "other":
        return row
    before_text = git.file_at(base, path) if base else None
    after_text = git.file_at(sha, path)
    before = _stats(before_text)
    after = _stats(after_text)
    before_decls = _decl_counts(category, before_text)
    after_decls = _decl_counts(category, after_text)
    row.update({
        "loc_before": before["loc"], "loc_after": after["loc"],
        "loc_code_before": before["loc_code"], "loc_code_after": after["loc_code"],
        "sorry_before": before["sorries"], "sorry_after": after["sorries"],
        "sorry_delta": after["sorries"] - before["sorries"],
        "decl_before": before_decls,
        "decl_after": after_decls,
        "decl_delta": _count_delta(before_decls, after_decls),
        "added": before["loc"] == 0 and after["loc"] > 0,
        "deleted": after["loc"] == 0 and before["loc"] > 0,
    })
    return row


def _rollup(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decl_after: dict[str, int] = {}
    decl_delta: dict[str, int] = {}
    for row in rows:
        for key, value in row.get("decl_after", {}).items():
            decl_after[key] = decl_after.get(key, 0) + int(value)
        for key, value in row.get("decl_delta", {}).items():
            decl_delta[key] = decl_delta.get(key, 0) + int(value)
    return {
        "files": len(rows),
        "add": sum(r.get("add", 0) for r in rows),
        "del": sum(r.get("del", 0) for r in rows),
        "loc_after": sum(r.get("loc_after", 0) for r in rows),
        "loc_code_after": sum(r.get("loc_code_after", 0) for r in rows),
        "loc_delta": sum(r.get("loc_after", 0) - r.get("loc_before", 0) for r in rows),
        "loc_code_delta": sum(r.get("loc_code_after", 0) - r.get("loc_code_before", 0) for r in rows),
        "sorry_after": sum(r.get("sorry_after", 0) for r in rows),
        "sorry_delta": sum(r.get("sorry_delta", 0) for r in rows),
        "decl_after": decl_after,
        "decl_delta": {k: v for k, v in sorted(decl_delta.items()) if v},
    }


def session_change_summary(
    root: Path,
    sha: str | None,
    project_paths: tuple[str, ...],
    *,
    base: str | None = None,
) -> dict[str, Any]:
    """Change summary for the commit ``sha``, scoped to ``project_paths``
    (workspace-relative directory prefixes).

    ``base`` is the commit to diff against; pass None (the normal case) to use
    this commit's own git PARENT — exactly what git records the commit as
    changing. The workspace ledger is ONE shared branch that every run commits
    onto, so a commit's parent is the ledger state immediately before it (it may
    belong to another run or a dashboard publish — fine, the diff still shows
    only what this commit changed). Only when there is no parent at all (the
    repo's first commit) is the result flagged ``initial`` for the UI to explain.

    Per-file rows carry LOC (total + code) and sorry counts before/after; the
    roll-ups (``sorry_delta``, ``loc_code_delta``, …) are Lean-only, and a
    parallel set is provided for blueprint (`.tex`) files. Diffs are fetched
    lazily per file (see :func:`session_file_diff`), not inlined here.
    """
    empty_files: list[dict[str, Any]] = []

    def _empty(reason: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "sha": sha,
            "files": empty_files,
            "lean": _rollup([r for r in empty_files if r["category"] == "lean"]),
            "blueprint": _rollup([r for r in empty_files if r["category"] == "blueprint"]),
            "other_count": sum(1 for r in empty_files if r["category"] == "other"),
            # Back-compat roll-up used by the run trend (Lean sorries).
            "sorry_delta": 0,
            "loc_add": 0,
            "loc_del": 0,
            "lean_files_changed": sum(1 for r in empty_files if r["category"] == "lean"),
        }

    if not sha:
        # The session's integration commit was a no-op — nothing changed.
        return _empty("no-changes")
    if not git_available():
        return _empty("no-git")

    git = WorkspaceGit(root)
    if not git.is_repo():
        return _empty("no-vcs")

    # Diff against the commit's own git parent (the ledger state right before
    # it) unless an explicit base is given — this is exactly what the commit
    # changed, and it is robust to the shared ledger branch interleaving many
    # runs' commits.
    if base is not None:
        base_source = "explicit-base"
    else:
        base = git.parent_sha(sha)
        base_source = "git-parent" if base is not None else "none"
    initial = base is None

    files = [_file_row(git, base, sha, add, dele, path) for add, dele, path in git.numstat(base, sha, project_paths)]
    files.sort(key=lambda r: (
        {"lean": 0, "blueprint": 1, "other": 2}[r["category"]],
        r.get("sorry_delta", 0),
        r["path"],
    ))
    lean = _rollup([r for r in files if r["category"] == "lean"])
    blueprint = _rollup([r for r in files if r["category"] == "blueprint"])
    return {
        "available": True,
        "initial": initial,
        "base_source": base_source,
        "sha": sha,
        "base": base,
        "files": files,
        "lean": lean,
        "blueprint": blueprint,
        "other_count": sum(1 for r in files if r["category"] == "other"),
        "sorry_delta": lean["sorry_delta"],
        "loc_add": lean["add"],
        "loc_del": lean["del"],
        "lean_files_changed": lean["files"],
    }


def session_file_diff(
    root: Path, sha: str | None, path: str, *, base: str | None = None
) -> dict[str, Any]:
    """Unified diff of one file at ``sha`` vs its parent (or ``base``), capped
    in size — the click-to-diff behind a commit card."""
    if not git_available() or not sha:
        return {"path": path, "available": False, "diff": ""}
    git = WorkspaceGit(root)
    if not git.is_repo():
        return {"path": path, "available": False, "diff": ""}
    if base is None:
        base = git.parent_sha(sha)
    text = git.diff(base, sha, (path,))
    truncated = len(text) > _MAX_FILE_DIFF_CHARS
    if truncated:
        text = text[:_MAX_FILE_DIFF_CHARS] + "\n… diff truncated …\n"
    return {"path": path, "available": True, "diff": text, "truncated": truncated}


