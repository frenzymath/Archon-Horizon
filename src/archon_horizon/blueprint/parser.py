"""Parse the leanblueprint LaTeX subset into a :class:`Blueprint`.

Ported from Archon's proven client-side renderer (``BlueprintRendered.tsx``):
the same comment stripping, nesting-aware environment matching, and metadata
extraction, so one parser can feed both the DAG (here) and an HTML renderer
later. Pure stdlib — no plasTeX, no LaTeX engine.
"""

from __future__ import annotations

import re
from dataclasses import replace

from .model import Blueprint, BlueprintNode

# amsthm-style environments leanblueprint uses; mirrors ENV_LABELS upstream.
KNOWN_ENVS: tuple[str, ...] = (
    "theorem",
    "lemma",
    "proposition",
    "corollary",
    "definition",
    "remark",
    "example",
    "proof",
    "notation",
    "convention",
)

_BEGIN_RE = re.compile(r"\\begin\{(" + "|".join(KNOWN_ENVS) + r")\}")
# No ``^`` anchor: this is matched with ``match(src, pos)``, which already
# anchors at ``pos`` — a ``^`` would (without MULTILINE) only fire at index 0.
_TITLE_RE = re.compile(r"\s*\[([^\]]*)\]")
_LEAN_RE = re.compile(r"\\lean\s*\{([^{}]*)\}")
_LABEL_RE = re.compile(r"\\label\s*\{([^{}]*)\}")
_USES_RE = re.compile(r"\\uses\s*\{([^{}]*)\}")
_SOURCE_RE = re.compile(r"\\source\s*\{([^{}]*)\}")
_LEANOK_RE = re.compile(r"\\leanok\b")
_NOTREADY_RE = re.compile(r"\\notready\b")
_MATHLIBOK_RE = re.compile(r"\\mathlibok\b")


def _strip_trailing_comment(line: str) -> str:
    """Drop a trailing ``%`` comment from one line, honouring escaped ``\\%``."""
    out: list[str] = []
    for i, ch in enumerate(line):
        if ch == "%" and (i == 0 or line[i - 1] != "\\"):
            break
        out.append(ch)
    return "".join(out)


def strip_comments(src: str) -> str:
    return "\n".join(_strip_trailing_comment(line) for line in src.split("\n"))


def _find_end_of_env(src: str, start_idx: int, name: str) -> int:
    """Index just past the matching ``\\end{name}`` for a ``\\begin{name}``.

    Nesting-aware (handles same-name environments inside). Returns -1 if
    unbalanced.
    """
    begin_tag = f"\\begin{{{name}}}"
    end_tag = f"\\end{{{name}}}"
    depth = 1
    i = start_idx
    while i < len(src):
        next_begin = src.find(begin_tag, i)
        next_end = src.find(end_tag, i)
        if next_end == -1:
            return -1
        if next_begin != -1 and next_begin < next_end:
            depth += 1
            i = next_begin + len(begin_tag)
        else:
            depth -= 1
            if depth == 0:
                return next_end + len(end_tag)
            i = next_end + len(end_tag)
    return -1


def _strip_nested_envs(body: str) -> str:
    """Remove nested ``\\begin{env}…\\end{env}`` blocks from a node body.

    A node owns only its own metadata: the nested ``proof`` (parsed as its own
    node) must not leak its ``\\uses`` up into the enclosing theorem.
    """
    parts: list[str] = []
    i = 0
    while True:
        match = _BEGIN_RE.search(body, i)
        if match is None:
            parts.append(body[i:])
            return "".join(parts)
        parts.append(body[i : match.start()])
        end_idx = _find_end_of_env(body, match.end(), match.group(1))
        if end_idx == -1:
            parts.append(body[match.start() :])
            return "".join(parts)
        i = end_idx


def _extract_meta(body: str) -> tuple[str, dict[str, object]]:
    """Pull and strip the leanblueprint metadata commands out of ``body``."""
    meta: dict[str, object] = {
        "lean": None,
        "label": None,
        "uses": [],
        "sources": [],
        "leanok": False,
        "notready": False,
        "mathlibok": False,
    }

    def take_lean(m: re.Match[str]) -> str:
        meta["lean"] = m.group(1).strip()
        return ""

    def take_label(m: re.Match[str]) -> str:
        meta["label"] = m.group(1).strip()
        return ""

    def take_uses(m: re.Match[str]) -> str:
        for tok in (t.strip() for t in m.group(1).split(",")):
            if tok:
                meta["uses"].append(tok)  # type: ignore[union-attr]
        return ""

    def take_source(m: re.Match[str]) -> str:
        for tok in (t.strip() for t in m.group(1).split(",")):
            if tok:
                meta["sources"].append(tok)  # type: ignore[union-attr]
        return ""

    body = _LEAN_RE.sub(take_lean, body)
    body = _LABEL_RE.sub(take_label, body)
    body = _USES_RE.sub(take_uses, body)
    body = _SOURCE_RE.sub(take_source, body)
    body, n_ok = _LEANOK_RE.subn("", body)
    body, n_nr = _NOTREADY_RE.subn("", body)
    body, n_ml = _MATHLIBOK_RE.subn("", body)
    meta["leanok"] = n_ok > 0
    meta["notready"] = n_nr > 0
    meta["mathlibok"] = n_ml > 0
    return body.strip(), meta


def parse_blueprint(source: str) -> Blueprint:
    src = strip_comments(source)
    nodes: list[BlueprintNode] = []
    synth = 0

    for match in _BEGIN_RE.finditer(src):
        name = match.group(1)
        cursor = match.end()

        title_match = _TITLE_RE.match(src, cursor)
        title = None
        if title_match:
            title = title_match.group(1).strip() or None
            cursor = title_match.end()

        end_idx = _find_end_of_env(src, cursor, name)
        if end_idx == -1:
            continue
        body = src[cursor : end_idx - len(f"\\end{{{name}}}")]

        statement, meta = _extract_meta(_strip_nested_envs(body))

        # A ``proof`` is not a standalone node: leanblueprint attaches its
        # ``\uses`` (extra dependencies) and ``\leanok`` (proof formalised) to
        # the statement it proves — the most recent node. Folding it in here
        # avoids the spurious ``node-N`` "PROOF" nodes the DAG would otherwise
        # show. A leading proof with no statement to attach to is dropped.
        if name == "proof":
            if nodes:
                parent = nodes[-1]
                nodes[-1] = replace(
                    parent,
                    uses=tuple(dict.fromkeys((*parent.uses, *meta["uses"]))),  # type: ignore[misc]
                    sources=tuple(dict.fromkeys((*parent.sources, *meta["sources"]))),  # type: ignore[misc]
                    leanok=parent.leanok or bool(meta["leanok"]),
                    notready=parent.notready or bool(meta["notready"]),
                )
            continue

        label = meta["label"]
        if not label:
            synth += 1
            label = f"node-{synth}"

        nodes.append(
            BlueprintNode(
                id=str(label),
                kind=name,
                statement=statement,
                title=title,
                lean=meta["lean"],  # type: ignore[arg-type]
                uses=tuple(meta["uses"]),  # type: ignore[arg-type]
                sources=tuple(meta["sources"]),  # type: ignore[arg-type]
                leanok=bool(meta["leanok"]),
                notready=bool(meta["notready"]),
                mathlibok=bool(meta["mathlibok"]),
            )
        )

    return Blueprint(nodes=tuple(nodes))
