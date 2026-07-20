"""List and read a project's Lean source files for the dashboard's Lean view.

Strictly scoped to the project root (no ``..`` traversal); build/cache dirs are
skipped so the tree shows only authored Lean.
"""

from __future__ import annotations

from pathlib import Path

_MAX_FILE_BYTES = 2 * 1024 * 1024
_SKIP_DIRS = {".lake", "_lake", ".archon", ".archon-horizon", ".git", "node_modules", "lake-packages"}

_IDENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_!?'")


def _scan(text: str) -> tuple[str, int]:
    """Single pass over Lean source → (code-only text, standalone `sorry` count).

    Mirrors the frontend's sorryScanner so the file-list count matches the
    in-file outline: handles `--` line comments, *nested* `/- -/` block comments
    and string literals (a regex stripper mis-nests, e.g. a stray `/-` inside a
    comment swallowing later real code). Comment characters are dropped from the
    code text (newlines kept) so blank/comment lines don't count as code.
    """
    n = len(text)
    block_depth = 0
    in_string = False
    in_line_comment = False
    sorries = 0
    out: list[str] = []
    i = 0
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_line_comment:
            if c == "\n":
                in_line_comment = False
                out.append("\n")
            i += 1
            continue
        if block_depth > 0:
            if c == "/" and nxt == "-":
                block_depth += 1
                i += 2
                continue
            if c == "-" and nxt == "/":
                block_depth -= 1
                i += 2
                continue
            if c == "\n":
                out.append("\n")
            i += 1
            continue
        if in_string:
            out.append(c)
            if c == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if c == '"':
                in_string = False
            i += 1
            continue
        if c == "-" and nxt == "-":
            in_line_comment = True
            i += 2
            continue
        if c == "/" and nxt == "-":
            block_depth = 1
            i += 2
            continue
        if c == '"':
            in_string = True
            out.append(c)
            i += 1
            continue
        if text.startswith("sorry", i):
            prev = text[i - 1] if i > 0 else ""
            after = text[i + 5] if i + 5 < n else ""
            if prev not in _IDENT and after not in _IDENT:
                sorries += 1
                out.append("sorry")
                i += 5
                continue
        out.append(c)
        i += 1
    return "".join(out), sorries


def count_sorries(text: str) -> int:
    """Number of standalone `sorry` tokens, ignoring comments and strings."""
    return _scan(text)[1]


def file_stats(text: str) -> dict:
    """Per-file metrics: total lines, code lines (no comments/blanks), sorries."""
    code, sorries = _scan(text)
    loc = len(text.splitlines())
    loc_code = sum(1 for line in code.splitlines() if line.strip())
    return {"loc": loc, "loc_code": loc_code, "sorries": sorries}


def list_lean_files(project_path: Path) -> list[dict]:
    """Relative ``.lean`` paths with size, LOC and sorry counts, sorted by path."""
    out: list[dict] = []
    if not project_path.is_dir():
        return out
    for path in project_path.rglob("*.lean"):
        if any(part in _SKIP_DIRS for part in path.relative_to(project_path).parts):
            continue
        try:
            text = path.read_text("utf-8", errors="replace")
            out.append({
                "path": path.relative_to(project_path).as_posix(),
                "size": path.stat().st_size,
                **file_stats(text),
            })
        except OSError:
            continue
    out.sort(key=lambda f: f["path"])
    return out


def read_lean_file(project_path: Path, rel: str) -> dict:
    """Text of one ``.lean`` file, with path-traversal and size guards."""
    if not rel or not rel.endswith(".lean"):
        raise ValueError("only .lean files are served")
    target = (project_path / rel).resolve()
    if project_path.resolve() != target and project_path.resolve() not in target.parents:
        raise ValueError("path escapes the project")
    if not target.is_file():
        raise ValueError("not found")
    size = target.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ValueError(f"file too large ({size} bytes)")
    return {"path": rel, "size": size, "content": target.read_text("utf-8", errors="replace")}
