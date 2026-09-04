"""Serve a project's blueprint as ordered chapters for the textbook reader.

Unlike the DAG path (hgraph nodes/edges), this returns chapter LaTeX plus a
merged KaTeX macro map and the document title/author so the dashboard can render
the blueprint like a textbook.

Chapter order and membership follow **hgraph**: expand the configured blueprint
entry (``content.tex`` / ``web.tex`` / ``print.tex``) with recursive ``\\input``,
then split on ``\\chapter``. Files under ``chapters/`` that are not reached from
that entry are not chapters. Preamble-only commands (``\\newcommand``,
``\\newtheorem``, …) are stripped from bodies the same way hgraph's
``parse_document`` does, so macro definitions never leak as prose.
"""

from __future__ import annotations

import re
from pathlib import Path

from archon_horizon.core.workspace import Workspace
from archon_horizon.hgraph.sync import (
    _HEAD_RE,
    _brace_span,
    _strip_definitions,
    read_blueprint,
)

from archon_horizon.hgraph.dashboard import discover_bib

from .hgraph_graph import _detect_entry
from .workspace import _find_blueprint_dir

# Where \title / \author typically live (the leanblueprint entry preambles).
_TITLE_CANDIDATES = ("web.tex", "print.tex", "content.tex")

_MACRO_RE = re.compile(r"\\(newcommand|renewcommand|providecommand|DeclareMathOperator)\*?\s*")
# Chapter files often reopen with \section{Same title} under a \chapter{…}.
# A leading \label{…} is kept so the dashboard can resolve \ref/\cref to the
# chapter (stripping it here used to leave every \cref{chap:…} broken).
_LEADING_HEADING_RE = re.compile(
    r"^\s*\\(?:chapter|section|subsection)\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}\s*"
    r"(?:\\label\s*\{[^{}]*\}\s*)?"
)
# Leftover brace groups from incompletely-scanned preamble cmds (e.g. {\par}).
_JUNK_ONLY_RE = re.compile(r"^(\s*\{[^{}]*\}\s*)+$")


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


def _slugify(title: str, used: set[str]) -> str:
    """Stable URL-ish slug from a chapter title; disambiguate collisions."""
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "chapter"
    slug = base
    n = 2
    while slug in used:
        slug = f"{base}-{n}"
        n += 1
    used.add(slug)
    return slug


def _is_empty_body(tex: str) -> bool:
    """True when stripped body has no readable content (preamble leftovers)."""
    s = tex.strip()
    if not s:
        return True
    return bool(_JUNK_ONLY_RE.match(s))


def _split_chapters(expanded: str) -> list[dict]:
    """Split an expanded blueprint into ``{slug, title, tex}`` chapters.

    Mirrors hgraph ``parse_document``'s chapter boundaries: only ``\\chapter``
    (starred or not) starts a new unit. Material before the first chapter is
    kept as an Introduction when it still has prose after stripping definitions.
    """
    # Drop \begin{document}...\end{document} wrapper when present.
    doc = re.search(r"\\begin\{document\}(.*)\\end\{document\}", expanded, re.DOTALL)
    if doc:
        expanded = doc.group(1)

    # Chapter markers only (level 1).
    markers: list[tuple[int, int, str, bool]] = []
    for m in _HEAD_RE.finditer(expanded):
        if m.group(1) != "chapter":
            continue
        content, end = _brace_span(expanded, m.end() - 1)
        title = re.sub(r"\s+", " ", content).strip()
        starred = bool(m.group(2))
        markers.append((m.start(), end, title, starred))

    used_slugs: set[str] = set()
    chapters: list[dict] = []

    def emit(title: str, body: str, *, starred: bool = False) -> None:
        body = _strip_definitions(body)
        # Drop a leading \section{…} only when it restates this chapter's title
        # (common leanblueprint pattern). A differently-titled first heading
        # (e.g. Schur's \subsection{…} under a broader \chapter) is kept.
        # Keep a leading \label{…} so \cref{chap:…} can resolve in the UI.
        hm = _LEADING_HEADING_RE.match(body)
        if hm and re.sub(r"\s+", " ", hm.group(1)).strip() == title:
            body = body[hm.end():]
        body = body.strip()
        if _is_empty_body(body):
            return
        slug = _slugify(title, used_slugs)
        chapters.append({
            "slug": slug,
            "title": title,
            "tex": body,
            **({"starred": True} if starred else {}),
        })

    if not markers:
        # Single-file / section-only blueprint: one synthetic chapter.
        emit("Blueprint", expanded)
        return chapters

    # Prose before the first \chapter (after stripping preamble junk).
    emit("Introduction", expanded[: markers[0][0]])

    for i, (_start, end, title, starred) in enumerate(markers):
        stop = markers[i + 1][0] if i + 1 < len(markers) else len(expanded)
        emit(title, expanded[end:stop], starred=starred)

    return chapters


def _discover_macros(bp_dir: Path, expanded: str) -> dict[str, str]:
    """KaTeX macros: every ``.tex``/``.sty`` under the blueprint tree, then the
    expanded entry (so ``\\input{macros}`` definitions are included once).

    First definition wins (provide-style), matching hgraph's discover_macros.
    """
    macros: dict[str, str] = {}
    if bp_dir.is_dir():
        files = sorted(list(bp_dir.rglob("*.tex")) + list(bp_dir.rglob("*.sty")))
        for f in files:
            try:
                for k, v in parse_macros(f.read_text("utf-8", errors="replace")).items():
                    macros.setdefault(k, v)
            except OSError:
                continue
    for k, v in parse_macros(expanded).items():
        macros.setdefault(k, v)
    return macros


def _doc_title_author(bp_dir: Path) -> tuple[str | None, str | None]:
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
    return doc_title, doc_author


def project_chapters(workspace: Workspace, name: str) -> dict:
    """Ordered blueprint chapters + macros + title for one project.

    Shape: ``{chapters: [{slug, title, tex}], macros, bib, docTitle, docAuthor,
    hasBlueprint, error}``. ``hasBlueprint`` is False (with an ``error``) when no
    blueprint entry is found. ``bib`` is the parsed ``.bib`` list used to render
    ``\\cite`` / the bibliography pane.

    Chapters come only from the configured blueprint entry's ``\\input`` tree
    (same entry detection as hgraph). Loose ``chapters/*.tex`` files that are not
    reached from that entry are omitted.
    """
    empty: dict = {
        "chapters": [],
        "macros": {},
        "bib": [],
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
        proj_root = None  # type: ignore[assignment]
    if not bp_dir or proj_root is None:
        return {**empty, "error": f"No blueprint found for project {name!r}."}

    entry = _detect_entry(proj_root, bp_dir)
    if entry is None or not entry.is_file():
        return {
            **empty,
            "error": (
                f"No blueprint entry (content.tex / web.tex / print.tex) "
                f"for project {name!r}."
            ),
        }

    try:
        expanded = read_blueprint(entry)
    except OSError as exc:
        return {**empty, "error": f"Failed to read blueprint entry for {name!r}: {exc}"}

    chapters = _split_chapters(expanded)
    if not chapters:
        return {
            **empty,
            "error": f"Blueprint entry for project {name!r} expanded to no chapters.",
        }

    doc_title, doc_author = _doc_title_author(bp_dir)
    macros = _discover_macros(bp_dir, expanded)
    # discover_bib walks the blueprint tree for *.bib (same helper as hgraph site).
    bib = discover_bib(str(entry))

    # API shape: only slug/title/tex (starred is optional metadata the UI ignores).
    public_chapters = [
        {"slug": ch["slug"], "title": ch["title"], "tex": ch["tex"]}
        for ch in chapters
    ]

    return {
        "chapters": public_chapters,
        "macros": macros,
        "bib": bib,
        "docTitle": doc_title,
        "docAuthor": doc_author,
        "hasBlueprint": True,
        "error": None,
    }
