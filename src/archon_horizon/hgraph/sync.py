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

from .graph import Graph, node_id


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
}
# when several edges land on one ordered pair, the strongest type wins
# (the hard `uses` edge subsumes the soft `formalizes` one); higher rank wins.
_EDGE_RANK = {"uses": 2, "formalizes": 1}
_DECL_RE = re.compile(
    r"^\s*(?:@\[[^\]]*\]\s*)?"                      # optional attribute
    r"(?:private\s+|protected\s+|noncomputable\s+)*"
    r"(theorem|lemma|def|abbrev|instance)\s+"
    r"([A-Za-z0-9_'.]+)"
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


def _strip_macros(text: str) -> str:
    # structural / provenance markers — captured into metadata, never body text
    text = re.sub(r"\\(label|lean|uses|proves|group|level|dcref|source)\{.*?\}", "", text, flags=re.DOTALL)
    text = re.sub(r"\\(leanok|notready|mathlibok)\b", "", text)
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
    def _brace_body(i: int) -> str:
        depth = 0
        for k in range(i, len(text)):
            if text[k] == "{":
                depth += 1
            elif text[k] == "}":
                depth -= 1
                if depth == 0:
                    return text[i + 1:k]
        return text[i + 1:]

    headings = [(mh.start(), re.sub(r"\s+", " ", _brace_body(mh.end() - 1)).strip())
                for mh in re.finditer(r"\\chapter\*?\s*\{", text)]

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
    argument is a single value that may contain commas (``\\group{Comparison, …}``)."""
    m = re.search(r"\\" + macro + r"\{(.*?)\}", text, re.DOTALL)
    return m.group(1).strip() if m else None


def _statement_fields(env: str, title, inner: str) -> dict:
    labels = _macro_args("label", inner)
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
        "group": _first_arg("group", inner),     # \group{…} → semantic-cluster field
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
    for m in re.finditer(r"\\(chapter|section|subsection|subsubsection|paragraph)\*?\s*\{", text):
        content, end = _brace_span(text, m.end() - 1)
        markers.append((m.start(), end, "head", _HEAD[m.group(1)],
                        re.sub(r"\s+", " ", content).strip()))
    for m in re.finditer(r"\\begin\{(" + env_alt + r")\}(?:" + _OPT_TITLE + r")?(.*?)\\end\{\1\}", text, re.DOTALL):
        markers.append((m.start(), m.end(), "stmt", m.group(1), (m.group(2), m.group(3))))
    for m in re.finditer(r"\\begin\{proof\}(.*?)\\end\{proof\}", text, re.DOTALL):
        markers.append((m.start(), m.end(), "proof", None, m.group(1)))
    markers.sort(key=lambda x: x[0])

    chapters: list[dict] = []
    cur = {"title": "Introduction", "blocks": []}

    def prose(a: int, b: int):
        chunk = re.sub(r"(?<!\\)%.*", "", text[a:b])           # drop LaTeX line-comments
        chunk = re.sub(r"\\label\{[^}]*\}", "", chunk)         # anchors, not content
        chunk = re.sub(r"\\(maketitle|tableofcontents|newpage|clearpage)\b", "", chunk).strip()
        if chunk:
            cur["blocks"].append({"t": "prose", "tex": chunk})

    pos = 0
    for s, e, kind, meta, data in markers:
        if s < pos:                     # inside an already-consumed span
            continue
        prose(pos, s)
        if kind == "head" and meta == 1:
            if cur["blocks"]:
                chapters.append(cur)
            cur = {"title": data, "blocks": []}
        elif kind == "head":
            cur["blocks"].append({"t": "head", "level": meta, "title": data})
        elif kind == "stmt":
            env, (opt, inner) = meta, data
            cur["blocks"].append({"t": "stmt", **_statement_fields(env, opt, inner)})
        elif kind == "proof":
            cur["blocks"].append({"t": "proof", "tex": _strip_macros(
                re.sub(r"(?<!\\)%.*", "", data)).strip()})
        pos = e
    prose(pos, len(text))
    if cur["blocks"]:
        chapters.append(cur)
    return chapters


def _assoc_proofs(statements: list[dict], proofs: list[dict]) -> dict[str, set[str]]:
    """Map each statement label → the set of labels its proof ``\\uses``, folding
    each proof's ``\\leanok`` / ``\\mathlibok`` back into its statement. A proof
    with ``\\proves{lbl}`` binds to that label — any of the statement's labels,
    canonical or legacy alias, resolved to the canonical one — otherwise to the
    nearest preceding statement."""
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
                continue
            label = max(preceding, key=lambda s: s["pos"])["label"]
        else:
            label = canonical.get(label, label)
        proof_uses.setdefault(label, set()).update(pr["uses"])
        if label in by_label:
            by_label[label]["leanok"] |= pr["leanok"]
            by_label[label]["mathlibok"] |= pr["mathlibok"]
    return proof_uses


def _tex_lean_status(s: dict, lean_status: dict[str, str]) -> tuple[str, list[str]]:
    """A blueprint item's formalization state, derived from the *actual* Lean —
    an author's ``\\leanok`` is not trusted on its own.

    ``\\mathlibok`` → ``mathlib_ok`` (its ``\\lean`` targets live in Mathlib, which
    we don't scan, so this stays an asserted link). Otherwise the state is
    ground-truth: ``lean_ok`` only when the item has ``\\lean`` targets and *every*
    one resolves to a real, ``sorry``-free declaration in the scanned sources;
    ``sorry`` when some Lean exists but is incomplete or a target is missing; and
    ``empty`` when no target resolves — a forward reference to Lean not yet written."""
    if s["mathlibok"]:
        return "mathlib_ok", list(s["lean"])
    targets = s["lean"]
    resolved = [lean_status[n] for n in targets if n in lean_status]
    if targets and len(resolved) == len(targets) and "sorry" not in resolved:
        return "lean_ok", []
    if resolved:                       # some Lean exists, but incomplete or partial
        return "sorry", []
    return "empty", []                 # nothing resolves — Lean not written yet


# --------------------------------------------------------------------------- #
# Lean parsing
# --------------------------------------------------------------------------- #
def parse_lean(text: str) -> list[dict]:
    """Extract declarations from Lean source. Tracks ``namespace``/``end`` to
    build the fully-qualified name; captures a preceding ``/-- … -/`` doc
    comment as part of the body; flags a ``sorry``."""
    lines = text.splitlines()
    ns: list[str] = []
    decls: list[tuple[int, str, str]] = []   # (line index, fqname, kind)
    for i, line in enumerate(lines):
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
            decls.append((i, ".".join(ns + [name]), kind))

    # for each decl, find the top of a /-- … -/ doc comment sitting above it
    tops: list[int] = []
    for i, _fq, _kind in decls:
        top, j = i, i - 1
        while j >= 0 and lines[j].strip() == "":
            j -= 1
        if j >= 0 and lines[j].strip().endswith("-/"):
            while j >= 0 and "/--" not in lines[j]:
                j -= 1
            if j >= 0:
                top = j
        tops.append(top)

    out: list[dict] = []
    for k, (i, fq, kind) in enumerate(decls):
        # a decl's code stops where the NEXT decl's doc comment begins, so an
        # adjacent decl's doc doesn't leak into this one's body.
        end = tops[k + 1] if k + 1 < len(decls) else len(lines)
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
        })
    return out


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
            owned: dict) -> None:
    """Create the node, or overwrite exactly the owned fields — leaving every
    authored field (origin, tags, status, …) in place, clearing ``stale``, and
    unsetting any owned field that is now empty (e.g. a doc comment removed)."""
    clean = {k: v for k, v in owned.items() if v is not None}
    empty = [k for k, v in owned.items() if v is None]
    if g.has_node(nid):
        g.modify_node(nid, title=title, type=type, content=content,
                      set_meta=clean, unset=["stale", *empty])
    else:
        g.add_node(title, type=type, id=nid, content=content, **clean)


def sync(g: Graph, *, blueprint: str | None = None, lean_paths=(),
         root: str | Path = ".") -> dict:
    warnings: list[str] = []
    seen = {"blueprint": set(), "lean": set()}
    root_abs = Path(root).resolve()

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
    for rel, decls in parsed:
        for d in decls:
            nid = node_id("lean", d["fqname"])
            lean_id[d["fqname"]] = nid
            lean_status[d["fqname"]] = "sorry" if d["sorry"] else "lean_ok"
            _upsert(g, nid, title=d["fqname"], type="lean", content=d["body"],
                    owned={"content_type": LEAN_KINDS.get(d["kind"], d["kind"]),
                           "generated": "lean", "author": "sync", "decl": d["fqname"],
                           "lean_status": lean_status[d["fqname"]],
                           "file": rel, "docstring": d["doc"] or None})
            seen["lean"].add(nid)

    # 2. Blueprint nodes (keyed by \label) ---------------------------------- #
    gen_edges: list[tuple[str, str, str]] = []
    if blueprint:
        statements, proofs = parse_blueprint(read_blueprint(blueprint))
        proof_uses = _assoc_proofs(statements, proofs)
        # every \label on a statement (canonical + any legacy aliases) resolves
        # to the same node id, so \uses{}/\lean{} can target either one
        bp_id = {lbl: node_id("bp", s["label"]) for s in statements for lbl in s["labels"]}

        for i, s in enumerate(statements):
            status, mathlib_names = _tex_lean_status(s, lean_status)
            _upsert(g, bp_id[s["label"]], title=s["title"], type="tex",
                    content=s["body"],
                    owned={"content_type": s["content_type"],
                           "generated": "blueprint", "author": "sync",
                           "label": s["label"], "chapter": s["chapter"],
                           "order": i, "lean_status": status,
                           "mathlib_name": mathlib_names or None,
                           "group": s.get("group") or None,
                           "level": s.get("level") or None,
                           "ref": s.get("ref") or None})
            seen["blueprint"].add(bp_id[s["label"]])

        # edges — every endpoint is derivable, so no lookup table is needed
        for s in statements:
            src = bp_id[s["label"]]
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
                g.delete_edge(e.id)
        for (s, t), ty in pair_type.items():
            if (s, t) in authored:
                warnings.append(f"edge {s}→{t}: authored edge present, kept over generated {ty}")
                continue
            old = existing.get((s, t))
            if old is None:
                g.add_edge(s, t, ty, generated="blueprint")
            elif old.type != ty:
                # type changed — rewrite, carrying authored extras through
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
            g.modify_node(n.id, set_meta={"stale": True})
            stale += 1

    return {"blueprint": len(seen["blueprint"]), "lean": len(seen["lean"]),
            "edges": made, "stale": stale, "warnings": warnings}
