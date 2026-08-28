"""Parse leanblueprint ``.tex`` and Lean sources into the graph.

The blueprint and the Lean files are the *reference* for the mathematical
content and its structure; the graph adds the layer they cannot hold (status,
provenance, comments, failed attempts). ``sync`` is a one-way reconcile:

* **Two independent node populations.** A blueprint item is keyed by its LaTeX
  ``\\label``; a Lean declaration is keyed by its fully-qualified name. There is
  no 1-to-1 correspondence — ``\\lean{...}`` links them with a *many-to-many*
  ``formalizes`` edge. Both ids are ``sha1("<kind>:<key>")[:12]`` so they are
  uniform and opaque; the human-readable surface is the blueprint / Lean / UI.

* **Owned vs authored.** ``sync`` only ever writes the fields it owns (title,
  content_type, body, the derived ``lean_status``) and the edges it generated
  (tagged ``generated:``). Everything a human or agent added
  — ``origin`` / source, ``tags``, ``status``, comments, hand-drawn edges — is
  left untouched. Re-running ``sync`` is idempotent.

* **No silent deletes.** A node whose source key vanished is marked
  ``stale: true`` (keeping its comments and metadata), never removed.

The edge endpoints are *deterministic*: ``\\lean{Gauss.IsEven}`` seeds exactly
the string the Lean node is keyed on, so ``sync`` needs no cross-reference index.
"""

from __future__ import annotations

import re
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

from .graph import Graph, HGraphError, node_id


def load_config(root: str | Path) -> dict:
    """Read ``<root>/hgraph/config.yaml`` if present, so a bare ``sync`` (or
    ``dashboard``/``site``) knows where things are. Recognised keys (paths are
    relative to ``<root>``)::

        blueprint: blueprint/blueprint.tex
        lean: [Lean]                 # a path or a list of paths
        site:                        # optional — see hgraph.site's docstring
          title: ...
          subtitle: ...
          overview: overview.md
          repo: owner/name
          accent: '#B4530B'          # this project's colour (or a full theme:);
                                      # applies to its card + blueprint view
          tabs:                      # extra content tabs on its blueprint view
            - {id: people, label: People, content: people.md}

    Returns ``{"blueprint": <abs path or None>, "lean": [<abs path>, ...],
    "site": <the site: block, verbatim, or {}>}``.
    """
    root = Path(root)
    p = root / "hgraph" / "config.yaml"
    if not p.exists():
        return {"blueprint": None, "lean": [], "site": {}}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    bp = data.get("blueprint")
    lean = data.get("lean") or []
    if isinstance(lean, str):
        lean = [lean]
    return {
        "blueprint": str(root / bp) if bp else None,
        "lean": [str(root / l) for l in lean],
        "site": data.get("site") or {},
    }

# ── blueprint theorem-like environments → content_type ─────────────────────── #
THM_ENVS = {
    "definition": "definition", "dfn": "definition", "lemma": "lemma",
    "theorem": "theorem", "thm": "theorem", "proposition": "proposition",
    "prop": "proposition", "corollary": "corollary", "cor": "corollary",
    "remark": "remark", "conjecture": "conjecture", "example": "example",
    "claim": "claim", "fact": "fact",
    # labelled, \uses-referenced, but not formalization targets themselves
    "convention": "convention", "notation": "notation",
}
# Optional ``[title]`` after ``\begin{env}`` — bracket-balanced (one nesting
# level) so a ``]`` inside the title (e.g. ``[… $C^{[1/\epsilon]}$-topology]``)
# doesn't cut it short and split a ``$…$`` math span across title/body.
_OPT_TITLE = r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]"
# ── Lean declaration keywords → content_type ───────────────────────────────── #
LEAN_KINDS = {
    "theorem": "theorem", "lemma": "lemma", "def": "definition",
    "abbrev": "definition", "instance": "instance",
    "structure": "structure", "class": "class", "inductive": "inductive",
}
# when several edges land on one ordered pair, the strongest type wins
# (the hard `uses` edge subsumes the soft `formalizes` one); higher rank wins.
_EDGE_RANK = {"uses": 2, "formalizes": 1}
_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?"                      # optional attribute
    r"(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(theorem|lemma|def|abbrev|instance|structure|class|inductive)\s+"
    # Lean identifiers may contain Unicode letters and symbols.  Stop at the
    # punctuation that starts a declaration's binders/type/body instead of
    # restricting the name to ASCII (e.g. `curvatureOperator_ιMulti`).
    r"([^\s(:=]+)"
)


# --------------------------------------------------------------------------- #
# blueprint parsing
# --------------------------------------------------------------------------- #
def _macro_args(macro: str, text: str) -> list[str]:
    """All comma-split arguments of every ``\\macro{a, b}`` in ``text``."""
    out: list[str] = []
    for m in re.finditer(r"\\" + macro + r"\{(.*?)\}", text, re.DOTALL):
        out += [a.strip() for a in m.group(1).split(",") if a.strip()]
    return out


# Display-math environments. Their contents are *not* prose: a `\label` inside
# one belongs to an equation, and is what `\cref{eq:…}` resolves against, so it
# must survive the strippers below (and must not be mistaken for a statement's
# own label). Starred forms are unnumbered but hold labels just the same.
DISPLAY_ENVS = ("equation", "align", "alignat", "flalign", "gather",
                "multline", "eqnarray", "displaymath")
_MATH_SPAN_RE = re.compile(
    r"\\begin\{(" + "|".join(DISPLAY_ENVS) + r")(\*?)\}.*?\\end\{\1\2\}", re.DOTALL)


def _outside_math(text: str, fn) -> str:
    """Apply ``fn`` to every part of ``text`` that sits outside display math."""
    out, pos = [], 0
    for m in _MATH_SPAN_RE.finditer(text):
        out.append(fn(text[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(fn(text[pos:]))
    return "".join(out)


_LABEL_RE = re.compile(r"\\label\{.*?\}", re.DOTALL)


def _strip_labels(text: str) -> str:
    """Drop the ``\\label``\\s that are anchors for *this* block, keeping the ones
    inside display math — those name equations, which are numbered and
    cross-referenced in their own right."""
    return _outside_math(text, lambda t: _LABEL_RE.sub("", t))


def _strip_macros(text: str) -> str:
    # structural / provenance markers — captured into metadata, never body text.
    # `\group{…}` is retired: it carries no meaning any more, but it is still
    # swallowed here so a blueprint that hasn't dropped it yet shows no junk.
    text = _strip_labels(text)
    text = re.sub(r"\\(lean|uses|proves|group|level|dcref|source)\{.*?\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\\(leanok|notready|mathlibok|sketch)\b", "", text)
    return text.strip()


def read_blueprint(path: str | Path) -> str:
    """Read a blueprint `.tex`, inlining ``\\input{…}`` recursively (paths are
    relative to the including file, ``.tex`` optional) — so a `content.tex` that
    just ``\\input``\\s its chapters expands to the whole document."""
    path = Path(path)
    base = path.parent

    def repl(m: "re.Match") -> str:
        inc = base / m.group(1).strip()
        if inc.suffix != ".tex":
            inc = inc.with_suffix(".tex")
        return read_blueprint(inc) if inc.exists() else ""

    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?<!\\)%.*", "", text)          # strip LaTeX line-comments
    return re.sub(r"\\input\{([^}]*)\}", repl, text)


def parse_blueprint(text: str) -> tuple[list[dict], list[dict]]:
    """Return (statements, proofs). A *statement* is one theorem-like
    environment with a ``\\label``; a *proof* carries proof-side ``\\uses``."""
    # chapter headings, to attribute each statement to its chapter. Balanced-
    # brace scan so titles with nested braces (\texttt{…}, {\v C}) aren't cut off.
    headings = [(mh.start(), re.sub(r"\s+", " ", _brace_span(text, mh.end() - 1)[0]).strip())
                for mh in _HEAD_RE.finditer(text) if mh.group(1) == "chapter"]

    def chapter_at(pos: int) -> str | None:
        prev = [h for h in headings if h[0] < pos]
        return prev[-1][1] if prev else None

    env_alt = "|".join(map(re.escape, THM_ENVS))
    statements: list[dict] = []
    for m in re.finditer(
        r"\\begin\{(" + env_alt + r")\}(?:" + _OPT_TITLE + r")?(.*?)\\end\{\1\}",
        text, re.DOTALL,
    ):
        env, title, inner = m.group(1), m.group(2), m.group(3)
        if not _macro_args("label", inner):
            continue  # unlabeled → not addressable, skip
        f = _statement_fields(env, title, inner)
        f.update({"pos": m.start(), "chapter": chapter_at(m.start())})
        statements.append(f)

    proofs: list[dict] = []
    for m in re.finditer(r"\\begin\{proof\}(.*?)\\end\{proof\}", text, re.DOTALL):
        inner = m.group(1)
        proves = _macro_args("proves", inner)
        proofs.append({
            "pos": m.start(),
            "proves": proves[0] if proves else None,
            "uses": _macro_args("uses", inner),     # proof deps → uses
            "leanok": bool(re.search(r"\\leanok\b", inner)),
            "mathlibok": bool(re.search(r"\\mathlibok\b", inner)),
            "sketch": bool(re.search(r"\\sketch\b", inner)),
        })
    return statements, proofs


def _lift_title(title, body):
    """leanblueprint often puts the title as a leading ``[ … ]`` inside the
    environment (after ``\\leanok``) rather than in ``\\begin{env}[…]``."""
    if not title:
        mt = re.match(r"\s*" + _OPT_TITLE + r"\s*", body)
        if mt:
            return mt.group(1), body[mt.end():].lstrip()
    return title, body


def _brace_span(text: str, i: int) -> tuple[str, int]:
    """``text[i]`` is ``{``; return (contents, index just past the matching ``}``)."""
    depth = 0
    for k in range(i, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:k], k + 1
    return text[i + 1:], len(text)


def _first_arg(macro: str, text: str) -> str | None:
    """The raw (un-split) argument of the first ``\\macro{…}`` — for markers whose
    argument is a single value that may contain commas (``\\source{Lee, p. 42}``)."""
    m = re.search(r"\\" + macro + r"\{(.*?)\}", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _statement_fields(env: str, title, inner: str) -> dict:
    # a \label inside display math names an equation, not the statement
    labels = _macro_args("label", _MATH_SPAN_RE.sub("", inner))
    body = _strip_macros(inner)
    title, body = _lift_title(title, body)
    return {
        "label": labels[0] if labels else None,
        # a statement may carry more than one \label (e.g. a new semantic label
        # plus the original book's legacy label, kept so old \ref{}/\uses{} calls
        # still resolve) — every one of them must point back to this same node.
        "labels": labels,
        "title": (title or (labels[0] if labels else env)).strip(),
        "content_type": THM_ENVS[env],
        "lean": _macro_args("lean", inner),
        "uses": _macro_args("uses", inner),
        "leanok": bool(re.search(r"\\leanok\b", inner)),
        "mathlibok": bool(re.search(r"\\mathlibok\b", inner)),
        # Standalone hgraph retired groups, but Horizon still renders this axis.
        "group": _first_arg("group", inner),
        # \sketch → the argument is deliberately incomplete (a proof sketch, an
        # omitted routine verification). Not a status to be fixed by syncing —
        # an author's statement about the maths, surfaced to the reader as-is.
        "sketch": bool(re.search(r"\\sketch\b", inner)),
        "level": _first_arg("level", inner),      # \level{coarse|medium|fine} → granularity
        # Source-book provenance. `\dcref{…}` is the original spelling;
        # `\source{slug:page-0001}` is what downstream blueprints are authored
        # with, so accept both (dcref wins if a statement carries both). Both are
        # stripped from the body by the annotation regex above, so a spelling
        # that isn't captured here is discarded silently — the statement still
        # renders and only its citation quietly disappears.
        "ref": _first_arg("dcref", inner) or _first_arg("source", inner),
        "body": body,
    }


_HEAD = {"chapter": 1, "section": 2, "subsection": 3, "subsubsection": 4, "paragraph": 5}
# A sectioning command, with LaTeX's two modifiers: the ``*`` form (unnumbered)
# and the optional short title (``\chapter[Short]{The long one}``) — which used
# to make the heading unrecognisable, so the whole chapter leaked into prose.
_HEAD_RE = re.compile(
    r"\\(" + "|".join(_HEAD) + r")(\*?)\s*(?:\[(?:[^\[\]]|\[[^\[\]]*\])*\]\s*)?\{")

# Preamble-only commands: definitions and layout switches that are never part of
# the document's text. A blueprint whose entry file \input{macros} (rather than
# wrapping its body in \begin{document}) otherwise dumps the whole macro file
# into the first chapter as prose. The macros themselves are collected
# separately for KaTeX — see hgraph.dashboard.discover_macros.
_PREAMBLE_CMDS = (
    "newcommand", "renewcommand", "providecommand", "DeclareMathOperator",
    "def", "newtheorem", "theoremstyle", "declaretheorem", "usepackage",
    "documentclass", "newenvironment", "renewenvironment", "setlength",
    "newlength", "definecolor", "bibliographystyle", "title", "author", "date",
)
_PREAMBLE_RE = re.compile(r"\\(" + "|".join(_PREAMBLE_CMDS) + r")(?![A-Za-z])")
# standalone switches — no arguments to consume
_SWITCH_RE = re.compile(
    r"\\(maketitle|tableofcontents|newpage|clearpage|printbibliography"
    r"|frontmatter|mainmatter|backmatter)\b")


def _skip_ws(text: str, i: int) -> int:
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _strip_definitions(chunk: str) -> str:
    """Drop every ``\\newcommand``-like definition, arguments and all.

    The arguments have to be *scanned* rather than matched: a macro body is
    brace-balanced and routinely spans lines, so a regex either stops at the
    first ``}`` (leaking the tail as prose — the ``\\newcommand[1]S^#1`` garbage)
    or runs away. At most four argument tokens are consumed, which is what the
    longest of these commands takes (``\\newtheorem{env}[shared]{Title}[section]``).
    """
    out, pos = [], 0
    for m in _PREAMBLE_RE.finditer(chunk):
        if m.start() < pos:
            continue
        i = _skip_ws(chunk, m.end())
        if i < len(chunk) and chunk[i] == "*":       # \DeclareMathOperator*
            i = _skip_ws(chunk, i + 1)
        for n in range(4):
            if i < len(chunk) and chunk[i] == "{":
                _, i = _brace_span(chunk, i)
            elif i < len(chunk) and chunk[i] == "[":
                j = chunk.find("]", i)
                if j < 0:
                    break
                i = j + 1
            elif n == 0 and i < len(chunk) and chunk[i] == "\\":
                # the unbraced spelling, \newcommand\R{\mathbb R} — only ever the
                # first token, so a following ordinary macro is never swallowed
                k = i + 1
                while k < len(chunk) and chunk[k].isalpha():
                    k += 1
                i = max(k, i + 2)
            else:
                break
            i = _skip_ws(chunk, i)
        out.append(chunk[pos:m.start()])
        pos = i
    out.append(chunk[pos:])
    return _SWITCH_RE.sub("", "".join(out))


def parse_document(text: str) -> list[dict]:
    """Parse the *whole* blueprint into an ordered document: a list of chapters,
    each with a flat list of blocks — headings, prose, statements, and proofs — in
    source order. This is what a faithful (enriched) blueprint render needs, as
    opposed to :func:`parse_blueprint` which only pulls out the labelled statements."""
    doc = re.search(r"\\begin\{document\}(.*)\\end\{document\}", text, re.DOTALL)
    if doc:
        text = doc.group(1)
    env_alt = "|".join(map(re.escape, THM_ENVS))

    markers = []
    for m in _HEAD_RE.finditer(text):
        content, end = _brace_span(text, m.end() - 1)
        # a heading's \label(s) follow it, so `\cref{sec:…}`/`\cref{chap:…}`
        # can resolve to this heading's number
        labels: list[str] = []
        while True:
            lm = re.match(r"\s*\\label\{([^}]*)\}", text[end:])
            if not lm:
                break
            labels.append(lm.group(1).strip())
            end += lm.end()
        markers.append((m.start(), end, "head", _HEAD[m.group(1)],
                        (re.sub(r"\s+", " ", content).strip(), bool(m.group(2)), labels)))
    for m in re.finditer(r"\\appendix\b", text):
        markers.append((m.start(), m.end(), "appendix", None, None))
    for m in re.finditer(r"\\begin\{(" + env_alt + r")\}(?:" + _OPT_TITLE + r")?(.*?)\\end\{\1\}", text, re.DOTALL):
        markers.append((m.start(), m.end(), "stmt", m.group(1), (m.group(2), m.group(3))))
    for m in re.finditer(r"\\begin\{proof\}(.*?)\\end\{proof\}", text, re.DOTALL):
        markers.append((m.start(), m.end(), "proof", None, m.group(1)))
    markers.sort(key=lambda x: x[0])

    chapters: list[dict] = []
    # anything before the first \chapter — a real introduction, or (when the
    # blueprint has no \begin{document}) just the preamble, in which case
    # _strip_definitions empties it and no phantom chapter is emitted at all.
    cur = {"title": "Introduction", "blocks": []}
    appendix = False

    def prose(a: int, b: int):
        chunk = re.sub(r"(?<!\\)%.*", "", text[a:b])           # drop LaTeX line-comments
        chunk = _strip_labels(chunk)          # anchors, not content (equations keep theirs)
        chunk = _strip_definitions(chunk).strip()
        if chunk:
            cur["blocks"].append({"t": "prose", "tex": chunk})

    pos = 0
    for s, e, kind, meta, data in markers:
        if s < pos:                     # inside an already-consumed span
            continue
        prose(pos, s)
        if kind == "appendix":
            appendix = True             # applies from the next \chapter on
        elif kind == "head" and meta == 1:
            if cur["blocks"]:
                chapters.append(cur)
            title, starred, labels = data
            cur = {"title": title, "blocks": []}
            if starred:
                cur["starred"] = True
            if appendix:
                cur["appendix"] = True
            if labels:
                cur["labels"] = labels
        elif kind == "head":
            title, starred, labels = data
            cur["blocks"].append({"t": "head", "level": meta, "title": title,
                                  **({"starred": True} if starred else {}),
                                  **({"labels": labels} if labels else {})})
        elif kind == "stmt":
            env, (opt, inner) = meta, data
            cur["blocks"].append({"t": "stmt", **_statement_fields(env, opt, inner)})
        elif kind == "proof":
            cur["blocks"].append({"t": "proof",
                                  **({"sketch": True} if re.search(r"\\sketch\b", data) else {}),
                                  "tex": _strip_macros(
                                      re.sub(r"(?<!\\)%.*", "", data)).strip()})
        pos = e
    prose(pos, len(text))
    if cur["blocks"]:
        chapters.append(cur)
    return chapters


def _assoc_proofs(statements: list[dict], proofs: list[dict],
                  warnings: list[str] | None = None) -> dict[str, set[str]]:
    """Map each statement label → the set of labels its proof ``\\uses``, folding
    each proof's ``\\leanok`` / ``\\mathlibok`` / ``\\sketch`` back into its
    statement. A proof with ``\\proves{lbl}`` binds to that label — any of the
    statement's labels, canonical or legacy alias, resolved to the canonical one
    — otherwise to the nearest preceding statement."""
    by_label = {s["label"]: s for s in statements}
    # \proves{} may name a legacy alias (a statement's 2nd+ \label); fold it
    # to the canonical label or the proof's uses/leanok silently vanish
    canonical = {alias: s["label"] for s in statements for alias in s["labels"]}
    proof_uses: dict[str, set[str]] = {}
    for pr in proofs:
        label = pr["proves"]
        if label is None:
            preceding = [s for s in statements if s["pos"] < pr["pos"]]
            if not preceding:
                if warnings is not None and (pr["uses"] or pr["leanok"]
                                             or pr["mathlibok"] or pr["sketch"]):
                    warnings.append(
                        f"proof at byte {pr['pos']}: has no preceding statement "
                        r"and no \proves{...}; proof metadata ignored")
                continue
            label = max(preceding, key=lambda s: s["pos"])["label"]
        else:
            raw_label = label
            label = canonical.get(label, label)
            if label not in by_label and warnings is not None:
                warnings.append(
                    f"proof at byte {pr['pos']}: \\proves{{{raw_label}}} has no blueprint node")
        proof_uses.setdefault(label, set()).update(pr["uses"])
        if label in by_label:
            by_label[label]["leanok"] |= pr["leanok"]
            by_label[label]["mathlibok"] |= pr["mathlibok"]
            by_label[label]["sketch"] |= pr["sketch"]
    return proof_uses


def _tex_lean_status(s: dict, lean_status: dict[str, str]) -> tuple[str, list[str]]:
    """A blueprint item's formalization state, from BOTH the author's assertion
    and the *actual* Lean — neither is trusted on its own.

    ``\\mathlibok`` → ``mathlib_ok`` (its ``\\lean`` targets live in Mathlib, which
    we don't scan, so this stays an asserted link). Otherwise:

    * ``lean_ok`` requires the author's ``\\leanok`` **and** every ``\\lean`` target
      resolving to a real, ``sorry``-free declaration in the scanned sources. The
      ``\\leanok`` is required, not merely trusted: a node whose Lean happens to
      compile but whose author deliberately withheld ``\\leanok`` (because a
      review found it incomplete) must not be reported done (I-0410). The Lean
      scan still guards against a lying ``\\leanok`` sitting over a ``sorry``.
    * ``linked`` — every target resolves ``sorry``-free but the item carries no
      ``\\leanok``: the Lean is attached and compiles, but the blueprint has not
      certified it complete. A distinct, honest state between "done" and "has a
      sorry", counted as *not done* everywhere ``lean_ok``/``mathlib_ok`` are.
    * ``sorry`` when some Lean exists but is incomplete or a target is missing.
    * ``empty`` when no target resolves — a forward reference to Lean not yet
      written."""
    if s["mathlibok"]:
        return "mathlib_ok", list(s["lean"])
    targets = s["lean"]
    resolved = [lean_status[n] for n in targets if n in lean_status]
    all_sorry_free = bool(targets) and len(resolved) == len(targets) and "sorry" not in resolved
    if all_sorry_free and s["leanok"]:
        return "lean_ok", []
    if all_sorry_free:                 # compiles, but the author has not asserted \leanok
        return "linked", []
    if resolved:                       # some Lean exists, but incomplete or partial
        return "sorry", []
    return "empty", []                 # nothing resolves — Lean not written yet


def _unlabeled_statement_warnings(text: str) -> list[str]:
    """Warn when annotations sit on a theorem-like environment that cannot
    become a graph node because it has no statement label."""
    env_alt = "|".join(map(re.escape, THM_ENVS))
    warnings: list[str] = []
    for m in re.finditer(
        r"\\begin\{(" + env_alt + r")\}(?:" + _OPT_TITLE + r")?(.*?)\\end\{\1\}",
        text, re.DOTALL,
    ):
        env, inner = m.group(1), m.group(3)
        annotated = (_macro_args("lean", inner) or _macro_args("uses", inner)
                     or re.search(r"\\(leanok|mathlibok|sketch)\b", inner))
        if annotated and not _macro_args("label", _MATH_SPAN_RE.sub("", inner)):
            warnings.append(
                f"{env} at byte {m.start()}: unlabeled blueprint statement "
                "is not imported as a graph node")
    return warnings


def _label_warnings(statements: list[dict]) -> list[str]:
    warnings: list[str] = []
    owner: dict[str, str] = {}
    for s in statements:
        seen_here: set[str] = set()
        for label in s["labels"]:
            if label in seen_here:
                warnings.append(f"{s['label']}: duplicate \\label{{{label}}} on the same statement")
            elif label in owner and owner[label] != s["label"]:
                warnings.append(
                    f"{label}: blueprint label is used by more than one statement "
                    f"({owner[label]}, {s['label']})")
            else:
                owner[label] = s["label"]
            seen_here.add(label)
    return warnings


def _status_warnings(s: dict, lean_status: dict[str, str]) -> list[str]:
    warnings: list[str] = []
    if s["leanok"] and not s["lean"] and not s["mathlibok"]:
        warnings.append(f"{s['label']}: \\leanok present but no \\lean{{...}} target is attached")
    if s["mathlibok"] and not s["lean"]:
        warnings.append(f"{s['label']}: \\mathlibok present but no \\lean{{...}} target is attached")
    if s["mathlibok"]:
        for name in s["lean"]:
            if name in lean_status:
                warnings.append(
                    f"{s['label']}: \\mathlibok marks \\lean{{{name}}} as external, "
                    "but that declaration exists in scanned Lean sources")
    return warnings


# --------------------------------------------------------------------------- #
# Lean parsing
# --------------------------------------------------------------------------- #
def _lean_code_lines(lines: list[str]) -> list[str]:
    """Return a parallel list of ``lines`` with every comment span blanked to
    spaces, preserving length and column positions.

    Lean's grammar for keywords is context-free of comments, but the graph
    scanner works line-by-line, so a docstring or ``/- … -/`` block whose prose
    happens to begin with ``class``/``def``/``theorem`` used to be parsed as a
    real declaration — inventing ghost nodes like ``class can`` from a sentence
    ``…whether the class can carry a witness…`` (I-0472, I-0613). Blanking
    comments first means only genuine code is scanned for ``namespace``/``end``
    and declarations, while the original lines are still used for the body and
    the ``/-- … -/`` docstring capture. Handles nested ``/- … -/``, doc
    comments (``/-- … -/`` opens the same nesting), and ``--`` line comments;
    string literals are not special-cased (they were not before either)."""
    out: list[str] = []
    depth = 0  # block-comment nesting depth carried across lines
    for line in lines:
        chars = list(line)
        i, n = 0, len(line)
        while i < n:
            pair = line[i:i + 2]
            if depth == 0 and pair == "--":
                for k in range(i, n):     # line comment: blank to end of line
                    chars[k] = " "
                break
            if pair == "/-":
                depth += 1
                chars[i] = chars[i + 1] = " "
                i += 2
                continue
            if pair == "-/" and depth > 0:
                depth -= 1
                chars[i] = chars[i + 1] = " "
                i += 2
                continue
            if depth > 0:
                chars[i] = " "
            i += 1
        out.append("".join(chars))
    return out


def parse_lean(text: str) -> list[dict]:
    """Extract declarations from Lean source. Tracks ``namespace``/``end`` to
    build the fully-qualified name; captures a preceding ``/-- … -/`` doc
    comment as part of the body; flags a ``sorry``."""
    lines = text.splitlines()
    # Scan structure and declarations on comment-blanked lines so prose inside a
    # docstring/comment can never masquerade as a declaration; body and doc
    # capture below still read the original `lines`.
    code_lines = _lean_code_lines(lines)
    ns: list[str] = []
    decls: list[tuple[int, str, str, bool]] = []   # (line index, fqname, kind, private)
    for i, line in enumerate(code_lines):
        s = line.strip()
        m_ns = re.match(r"namespace\s+([A-Za-z0-9_.]+)", s)
        if m_ns:
            ns.append(m_ns.group(1))
            continue
        m_end = re.match(r"end\s+([A-Za-z0-9_.]+)", s)
        if m_end and ns and ns[-1] == m_end.group(1):
            ns.pop()
            continue
        m = _DECL_RE.match(line)
        if m:
            kind, name = m.group(1), m.group(2)
            is_private = bool(re.search(r"\bprivate\s+", line[:m.start(1)]))
            # `_root_.Foo` is explicitly outside the surrounding namespace;
            # retaining the marker would create a name that Lean cannot refer
            # to (`MorganTianLib._root_.Foo`).
            fqname = name.removeprefix("_root_.") if name.startswith("_root_.") \
                else ".".join(ns + [name])
            decls.append((i, fqname, kind, is_private))

    # for each decl, find the top of a /-- … -/ *doc* comment sitting above it.
    # Plain `/- … -/` (and module `/-! … -/`) must not count: walking upward past
    # them used to latch onto an earlier `/--` and set the *next* declaration's
    # body range to end before this one starts, emptying node bodies on sync.
    tops: list[int] = []
    for i, _fq, _kind, _private in decls:
        tops.append(_doc_comment_top(lines, i))

    out: list[dict] = []
    for k, (i, fq, kind, is_private) in enumerate(decls):
        # a decl's code stops where the NEXT decl's doc comment begins, so an
        # adjacent decl's doc doesn't leak into this one's body.
        if k + 1 < len(decls):
            end = tops[k + 1]
            # Belt-and-suspenders: a poisoned top must never empty this body.
            if end <= i:
                end = decls[k + 1][0]
        else:
            end = len(lines)
        code = lines[i:end]
        while code and (code[-1].strip() == ""
                        or re.match(r"\s*(end|namespace)\b", code[-1])):
            code.pop()
        body = "\n".join(code)
        doc = ""
        if tops[k] < i:                             # a /-- … -/ sat above the decl
            raw = "\n".join(lines[tops[k]:i]).strip()
            raw = re.sub(r"^/--", "", raw)
            raw = re.sub(r"-/\s*$", "", raw)
            doc = raw.strip()
        out.append({
            "fqname": fq,
            "kind": kind,
            "body": body,
            "doc": doc,
            "sorry": bool(re.search(r"\bsorry\b", body)),
            "private": is_private,
        })
    return out


def _doc_comment_top(lines: list[str], decl_line: int) -> int:
    """Start line of a ``/-- … -/`` doc immediately above ``decl_line``, else ``decl_line``.

    Only Lean *documentation* comments (``/--``) attach to the following
    declaration. Plain ``/- … -/`` and module docs ``/-! … -/`` are ignored so
    they cannot pull the body range of later decls backward across the file.
    """
    j = decl_line - 1
    while j >= 0 and lines[j].strip() == "":
        j -= 1
    if j < 0 or not lines[j].strip().endswith("-/"):
        return decl_line
    # Walk up to the opener of *this* block only (stop at the first `/-`).
    while j >= 0 and "/-" not in lines[j]:
        j -= 1
    if j < 0:
        return decl_line
    opener_at = lines[j].find("/-")
    if opener_at < 0:
        return decl_line
    # `/--` is the only decl-doc opener. `/-!` is a module doc; plain `/-` is not.
    if not lines[j][opener_at:].startswith("/--"):
        return decl_line
    return j


def _iter_lean_files(paths) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files += sorted(p.rglob("*.lean"))
        elif p.exists():
            files.append(p)
    return files


# below this many .lean files, spinning up a process pool costs more than it
# saves; above it, parse_lean's regex work is worth spreading across cores.
_PARALLEL_THRESHOLD = 8


def _read_and_parse_lean(args: tuple[Path, Path]) -> tuple[str, list[dict]]:
    """Read one .lean file and parse its declarations. Module-level (picklable)
    so it can run in a subprocess — reading and regex-parsing a file are pure
    functions of its content, safe to run in parallel across files."""
    f, root_abs = args
    try:
        rel = str(f.resolve().relative_to(root_abs))
    except ValueError:
        rel = str(f)
    return rel, parse_lean(f.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# the reconcile driver
# --------------------------------------------------------------------------- #
def _upsert(g: Graph, nid: str, *, title: str, type: str, content: str,
            owned: dict, dry_run: bool = False) -> bool:
    """Create the node, or overwrite exactly the owned fields — leaving every
    authored field (origin, tags, status, …) in place, clearing ``stale``, and
    unsetting any owned field that is now empty (e.g. a doc comment removed).

    Return whether the graph differs from the requested state.  ``dry_run``
    performs the same comparison without writing, which lets ``serve`` detect
    pending sync work using the reconciliation rules themselves.
    """
    clean = {k: v for k, v in owned.items() if v is not None}
    empty = [k for k, v in owned.items() if v is None]
    if g.has_node(nid):
        n = g.get_node(nid)
        changed = n.title != title or n.type != type or n.content != content
        changed |= any(n.meta.get(k) != v for k, v in clean.items())
        changed |= any(k in n.meta for k in ("stale", *empty))
        if changed and not dry_run:
            g.modify_node(nid, title=title, type=type, content=content,
                          set_meta=clean, unset=["stale", *empty])
    else:
        changed = True
        if not dry_run:
            g.add_node(title, type=type, id=nid, content=content, **clean)
    return changed


def sync(g: Graph, *, blueprint: str | None = None, lean_paths=(),
         root: str | Path = ".", dry_run: bool = False) -> dict:
    """Reconcile configured sources into ``g``.

    With ``dry_run=True`` all sources are parsed and the exact pending changes
    are counted, but the graph is left untouched.  The returned ``changes``
    count is zero precisely when a real sync would be a no-op.
    """
    warnings: list[str] = []
    seen = {"blueprint": set(), "lean": set()}
    root_abs = Path(root).resolve()
    lean_paths = tuple(lean_paths)

    if blueprint and not Path(blueprint).is_file():
        raise HGraphError(f"blueprint source not found: {blueprint}")
    missing_lean = [str(p) for p in lean_paths if not Path(p).exists()]
    if missing_lean:
        raise HGraphError("Lean source path(s) not found: " + ", ".join(missing_lean))

    node_changes = 0
    edge_changes = 0

    # 1. Lean nodes (keyed by fully-qualified name) ------------------------- #
    # reading + regex-parsing each file is independent of every other file,
    # so on a large Lean tree it's worth spreading across a process pool;
    # `_upsert` (graph writes) stays sequential below.
    lean_files = _iter_lean_files(lean_paths)
    if len(lean_files) >= _PARALLEL_THRESHOLD:
        with ProcessPoolExecutor() as ex:
            parsed = list(ex.map(_read_and_parse_lean,
                                 ((f, root_abs) for f in lean_files)))
    else:
        parsed = [_read_and_parse_lean((f, root_abs)) for f in lean_files]

    lean_id: dict[str, str] = {}
    lean_status: dict[str, str] = {}          # fqname → lean_ok | sorry
    lean_seen_at: dict[str, str] = {}
    lean_private: dict[str, bool] = {}
    for rel, decls in parsed:
        for d in decls:
            if d["fqname"] in lean_seen_at:
                warnings.append(
                    f"{d['fqname']}: Lean declaration appears more than once "
                    f"({lean_seen_at[d['fqname']]}, {rel})")
            else:
                lean_seen_at[d["fqname"]] = rel
            nid = node_id("lean", d["fqname"])
            lean_id[d["fqname"]] = nid
            lean_status[d["fqname"]] = "sorry" if d["sorry"] else "lean_ok"
            lean_private[d["fqname"]] = bool(d.get("private"))
            node_changes += _upsert(
                g, nid, title=d["fqname"], type="lean", content=d["body"],
                owned={"content_type": LEAN_KINDS.get(d["kind"], d["kind"]),
                       "generated": "lean", "author": "sync", "decl": d["fqname"],
                       "lean_status": lean_status[d["fqname"]],
                       "file": rel, "docstring": d["doc"] or None,
                       "private": True if d.get("private") else None},
                dry_run=dry_run)
            seen["lean"].add(nid)

    # 2. Blueprint nodes (keyed by \label) ---------------------------------- #
    gen_edges: list[tuple[str, str, str]] = []
    if blueprint:
        blueprint_text = read_blueprint(blueprint)
        warnings.extend(_unlabeled_statement_warnings(blueprint_text))
        statements, proofs = parse_blueprint(blueprint_text)
        warnings.extend(_label_warnings(statements))
        proof_uses = _assoc_proofs(statements, proofs, warnings)
        # every \label on a statement (canonical + any legacy aliases) resolves
        # to the same node id, so \uses{}/\lean{} can target either one
        bp_id = {lbl: node_id("bp", s["label"]) for s in statements for lbl in s["labels"]}

        for i, s in enumerate(statements):
            status, mathlib_names = _tex_lean_status(s, lean_status)
            node_changes += _upsert(
                g, bp_id[s["label"]], title=s["title"], type="tex",
                content=s["body"],
                owned={"content_type": s["content_type"],
                       "generated": "blueprint", "author": "sync",
                       "label": s["label"], "chapter": s["chapter"],
                       "order": i, "lean_status": status,
                       "mathlib_name": mathlib_names or None,
                       # Retained by Horizon's graph UI even though standalone
                       # hgraph no longer consumes this metadata field.
                       "group": s.get("group") or None,
                       "sketch": True if s.get("sketch") else None,
                       "level": s.get("level") or None,
                       "ref": s.get("ref") or None},
                dry_run=dry_run)
            seen["blueprint"].add(bp_id[s["label"]])

        # edges — every endpoint is derivable, so no lookup table is needed
        for s in statements:
            src = bp_id[s["label"]]
            warnings.extend(_status_warnings(s, lean_status))
            for name in s["lean"]:                          # \lean → formalizes
                if name in lean_id:
                    gen_edges.append((src, lean_id[name], "formalizes"))
                elif not s["mathlibok"]:                    # \mathlibok ⇒ external is expected
                    warnings.append(f"{s['label']}: \\lean{{{name}}} not found in Lean sources")
            for ref in s["uses"]:                           # statement \uses → uses
                if ref in bp_id:
                    gen_edges.append((src, bp_id[ref], "uses"))
                else:
                    warnings.append(f"{s['label']}: \\uses{{{ref}}} (statement) has no blueprint node")
            for ref in sorted(proof_uses.get(s["label"], ())):  # proof \uses → uses
                if ref in bp_id:
                    gen_edges.append((src, bp_id[ref], "uses"))
                else:
                    warnings.append(f"{s['label']}: \\uses{{{ref}}} (proof) has no blueprint node")

        referenced_lean = {name for s in statements for name in s["lean"] if name in lean_id}
        for name in sorted(lean_id):
            if not lean_private.get(name) and name not in referenced_lean:
                warnings.append(
                    f"{name}: Lean declaration is not referenced by any blueprint \\lean{{...}}")

    # 3. reconcile generated edges: one per ordered pair, collapsed to the
    #    strongest type, never overwriting an authored edge on that pair —
    #    and never touching an edge file that is already right, so authored
    #    fields (note:) and attachment dirs on generated edges survive, and
    #    re-running sync is a true no-op on unchanged sources. All generated
    #    edges derive from the blueprint, so a Lean-only sync leaves them
    #    alone entirely (deleting them all was NOT what "partial sync" means).
    made = 0
    if blueprint:
        pair_type: dict[tuple[str, str], str] = {}
        for s, t, ty in gen_edges:
            cur = pair_type.get((s, t))
            if cur is None or _EDGE_RANK.get(ty, 0) > _EDGE_RANK.get(cur, 0):
                pair_type[(s, t)] = ty
        authored = set()
        existing: dict[tuple[str, str], object] = {}
        for e in g.edges():
            if e.attrs.get("generated"):
                existing[(e.source, e.target)] = e
            else:
                authored.add((e.source, e.target))
        for pair, e in existing.items():
            if pair not in pair_type:            # vanished from the sources
                edge_changes += 1
                if not dry_run:
                    g.delete_edge(e.id)
        for (s, t), ty in pair_type.items():
            if (s, t) in authored:
                warnings.append(f"edge {s}→{t}: authored edge present, kept over generated {ty}")
                continue
            old = existing.get((s, t))
            if old is None:
                edge_changes += 1
                if not dry_run:
                    g.add_edge(s, t, ty, generated="blueprint")
            elif old.type != ty:
                # type changed — rewrite, carrying authored extras through
                edge_changes += 1
                if not dry_run:
                    extra = {k: v for k, v in old.attrs.items() if k != "generated"}
                    g.add_edge(s, t, ty, replace=True, generated="blueprint", **extra)
            made += 1

    # 4. mark vanished generated nodes stale (never delete) — only within the
    #    populations that were actually synced this run: a --lean-only sync
    #    must not declare every blueprint node stale (and vice versa) -------- #
    stale = 0
    for n in g.nodes():
        gen = n.meta.get("generated")
        synced = (gen == "blueprint" and bool(blueprint)) or \
                 (gen == "lean" and bool(lean_paths))
        if synced and n.id not in seen[gen] and not n.meta.get("stale"):
            if not dry_run:
                g.modify_node(n.id, set_meta={"stale": True})
            stale += 1

    return {"blueprint": len(seen["blueprint"]), "lean": len(seen["lean"]),
            "edges": made, "stale": stale, "warnings": warnings,
            "node_changes": node_changes, "edge_changes": edge_changes,
            "changes": node_changes + edge_changes + stale}


def sync_from_config(root: str | Path, *, dry_run: bool = False) -> dict:
    """Sync one project using its ``hgraph/config.yaml`` source settings."""
    cfg = load_config(root)
    if not cfg["blueprint"] and not cfg["lean"]:
        raise HGraphError(
            f"no blueprint or Lean sources configured in {Path(root) / 'hgraph/config.yaml'}")
    return sync(Graph.open(root), blueprint=cfg["blueprint"], lean_paths=cfg["lean"],
                root=root, dry_run=dry_run)


def project_sync_status(root: str | Path) -> dict:
    """Describe whether one project's generated graph matches its sources.

    Hand-authored graphs need no sync and are reported as ``manual``.  Missing,
    empty, unconfigured generated, and invalid projects are kept distinct so a
    workspace ``serve`` warning can tell the user what needs attention.
    """
    root = Path(root)
    graph_dir = root / "hgraph"
    if not graph_dir.is_dir():
        return {"state": "missing", "message": "no hgraph/ directory"}

    try:
        nodes = list(Graph.open(root).nodes())
    except Exception as e:
        return {"state": "error", "message": str(e)}

    try:
        cfg = load_config(root)
    except Exception as e:
        return {"state": "error", "message": str(e)}
    if not cfg["blueprint"] and not cfg["lean"]:
        if any(n.meta.get("generated") in ("blueprint", "lean") for n in nodes):
            return {"state": "unconfigured",
                    "message": "generated nodes exist, but no sync sources are configured"}
        if not nodes:
            return {"state": "empty", "message": "the graph has no nodes"}
        return {"state": "manual", "message": "authored graph (no sync configured)"}

    try:
        result = sync(Graph.open(root), blueprint=cfg["blueprint"],
                      lean_paths=cfg["lean"], root=root, dry_run=True)
    except Exception as e:
        return {"state": "error", "message": str(e)}
    return {
        "state": "out_of_sync" if result["changes"] else "in_sync",
        "message": (f"{result['changes']} generated graph change(s) pending"
                    if result["changes"] else "generated graph is current"),
        "result": result,
    }


def workspace_sync_status(manifest: dict, base: str | Path) -> list[dict]:
    """Return :func:`project_sync_status` for every project in a manifest."""
    projects = manifest.get("projects") if isinstance(manifest, dict) else None
    if not isinstance(projects, list):
        raise HGraphError("workspace manifest needs a projects: list")
    out = []
    for project in projects:
        if not isinstance(project, dict) or project.get("root") is None:
            raise HGraphError("each workspace project needs a root:")
        root = Path(base) / str(project["root"])
        out.append({"name": project.get("name") or str(project["root"]),
                    "root": str(project["root"]),
                    **project_sync_status(root)})
    return out
