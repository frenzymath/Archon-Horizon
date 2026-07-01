"""Serve a project's blueprint as ordered chapters for the textbook reader.

Unlike :mod:`archon_horizon.blueprint.dag`, which parses the blueprint into a
dependency graph of nodes, this returns the *raw* chapter LaTeX (comments
preserved) plus a merged KaTeX macro map and the document title/author, so the
dashboard can render the blueprint like a textbook (numbered chapters, sections,
theorems, cross-references) the way ``leanblueprint web`` would.

This mirrors Archon's ``/api/blueprint/chapters`` route, ported to Python.
"""

from __future__ import annotations

import re
from pathlib import Path

from archon_horizon.core.workspace import Workspace

from .workspace import _find_blueprint_dir

# Where \title / \author typically live (the leanblueprint entry preambles).
_TITLE_CANDIDATES = ("web.tex", "print.tex", "content.tex")

_MACRO_RE = re.compile(r"\\(newcommand|renewcommand|providecommand|DeclareMathOperator)\*?\s*")
_HEADING_RE = re.compile(r"\\(?:chapter|section)\*?\s*\{([^{}]*)\}")
_STRIP_HEADING_RE = re.compile(r"\\(?:chapter|section)\*?\s*\{[^{}]*\}\s*")
_INPUT_RE = re.compile(r"\\input\s*\{([^{}]+)\}")


def _strip_tex_comments(src: str) -> str:
    """Drop LaTeX line comments (an unescaped ``%`` to end of line)."""
    out: list[str] = []
    for line in src.split("\n"):
        i = 0
        cut = None
        while i < len(line):
            if line[i] == "\\":
                i += 2
                continue
            if line[i] == "%":
                cut = i
                break
            i += 1
        out.append(line if cut is None else line[:cut])
    return "\n".join(out)


def _match_brace(src: str, open_idx: int) -> int:
    """Index of the ``}`` matching the ``{`` at ``open_idx``, or -1."""
    depth = 0
    i = open_idx
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _braced_arg(src: str, cmd: str) -> str | None:
    """Balanced-brace argument of ``\\<cmd>{...}``, or None."""
    at = src.find(f"\\{cmd}")
    if at < 0:
        return None
    i = src.find("{", at)
    if i < 0:
        return None
    close = _match_brace(src, i)
    return src[i + 1 : close].strip() if close != -1 else None


def parse_macros(src: str) -> dict[str, str]:
    """KaTeX macro map from ``\\newcommand`` / ``\\DeclareMathOperator`` etc.

    Keys are the full command (e.g. ``\\R``), values the expansion. The optional
    arg-count ``[n]`` and default-value brackets are skipped — KaTeX auto-detects
    ``#n`` placeholders.
    """
    out: dict[str, str] = {}
    source = _strip_tex_comments(src)
    for m in _MACRO_RE.finditer(source):
        cmd_type = m.group(1)
        i = m.end()
        name: str | None = None
        if i < len(source) and source[i] == "{":
            close = _match_brace(source, i)
            if close == -1:
                continue
            inside = source[i + 1 : close].strip()
            nm = re.match(r"^\\([A-Za-z@]+)$", inside)
            if nm:
                name = nm.group(1)
            i = close + 1
        elif i < len(source) and source[i] == "\\":
            nm = re.match(r"^\\([A-Za-z@]+)", source[i:])
            if not nm:
                continue
            name = nm.group(1)
            i += len(nm.group(0))
        if not name:
            continue
        # Skip up to two optional [..] groups (arg count + default).
        for _ in range(2):
            while i < len(source) and source[i].isspace():
                i += 1
            if i < len(source) and source[i] == "[":
                close = source.find("]", i)
                if close == -1:
                    break
                i = close + 1
        while i < len(source) and source[i].isspace():
            i += 1
        if i >= len(source) or source[i] != "{":
            continue
        close = _match_brace(source, i)
        if close == -1:
            continue
        body = source[i + 1 : close]
        if cmd_type == "DeclareMathOperator":
            body = f"\\operatorname{{{body}}}"
        out[f"\\{name}"] = body
    return out


def _humanize(slug: str) -> str:
    return slug.replace("_", " / ").replace("-", " ")


def _title_of(tex: str, slug: str) -> str:
    m = _HEADING_RE.search(tex)
    return m.group(1).strip() if m else _humanize(slug)


def _strip_heading(tex: str) -> str:
    return _STRIP_HEADING_RE.sub("", tex, count=1)


def project_chapters(workspace: Workspace, name: str) -> dict:
    """Ordered blueprint chapters + macros + title for one project.

    Shape: ``{chapters: [{slug, title, tex}], macros, docTitle, docAuthor,
    hasBlueprint, error}``. ``hasBlueprint`` is False (with an ``error``) when no
    chapter sources are found.
    """
    empty: dict = {
        "chapters": [],
        "macros": {},
        "docTitle": None,
        "docAuthor": None,
        "hasBlueprint": False,
        "error": None,
    }
    try:
        proj = workspace.project(name)
        proj_root = workspace.root / proj.path
        bp_dir = _find_blueprint_dir(proj_root, proj.blueprint_path, workspace.root)
    except Exception:
        bp_dir = None
    if not bp_dir:
        return {**empty, "error": f"No blueprint found for project {name!r}."}

    chapters_dir = bp_dir / "chapters" if (bp_dir / "chapters").is_dir() else bp_dir
    chapter_files = sorted(p for p in chapters_dir.glob("*.tex"))
    if not chapter_files:
        return {**empty, "error": f"No blueprint chapters for project {name!r}."}

    # Title / author for the intro page.
    doc_title: str | None = None
    doc_author: str | None = None
    for cand in _TITLE_CANDIDATES:
        src_path = bp_dir / cand
        if not src_path.is_file():
            continue
        src = src_path.read_text("utf-8")
        doc_title = doc_title or _braced_arg(src, "title")
        doc_author = doc_author or _braced_arg(src, "author")
        if doc_title:
            break

    by_slug = {p.stem: p for p in chapter_files}

    # Reading order from content.tex \input{...}; unreferenced chapters appended.
    order: list[str] = []
    content_path = bp_dir / "content.tex"
    if content_path.is_file():
        for m in _INPUT_RE.finditer(content_path.read_text("utf-8")):
            slug = Path(m.group(1).strip()).name.removesuffix(".tex")
            if slug in by_slug and slug not in order:
                order.append(slug)
    for slug in by_slug:
        if slug not in order:
            order.append(slug)

    chapters: list[dict] = []
    for slug in order:
        raw = by_slug[slug].read_text("utf-8")
        chapters.append({"slug": slug, "title": _title_of(raw, slug), "tex": _strip_heading(raw)})

    # Macros: every macros/*.tex, then chapter-local \newcommand (provide-style:
    # the macros/ dir wins on collision).
    macros: dict[str, str] = {}
    macros_dir = bp_dir / "macros"
    if macros_dir.is_dir():
        for f in sorted(macros_dir.glob("*.tex")):
            macros.update(parse_macros(f.read_text("utf-8")))
    for ch in chapters:
        for k, v in parse_macros(ch["tex"]).items():
            macros.setdefault(k, v)

    return {
        "chapters": chapters,
        "macros": macros,
        "docTitle": doc_title,
        "docAuthor": doc_author,
        "hasBlueprint": True,
        "error": None,
    }
