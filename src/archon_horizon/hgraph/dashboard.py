"""Blueprint data — the full blueprint document as JSON, enriched with the graph.

Pure data, no rendering: numbered chapters, statements enriched with the
graph's data (`lean_status`, Lean declarations, dependencies, reviews,
comments). The React frontend (`frontend/`, shipped pre-built at
`hgraph/webui/`) does all the rendering — this module only ever produces
JSON, written by `hgraph site` or served live by `hgraph serve`.
"""

from __future__ import annotations

import re
from pathlib import Path

from .graph import Graph
from .sync import load_config, parse_document, read_blueprint


# --------------------------------------------------------------------------- #
# graph → data
# --------------------------------------------------------------------------- #
def _att(a) -> dict:
    return {"author": a.meta.get("author"),
            "title": a.meta.get("title"), "text": a.content,
            "created": a.meta.get("created") or a.meta.get("date"),
            "updated": a.meta.get("updated") or a.meta.get("date")}


def _review_att(a) -> dict:
    return {"author": a.meta.get("author"),
            "maths_verdict": a.meta.get("maths_verdict"),
            "maths_comment": a.meta.get("maths_comment"),
            "lean_verdict": a.meta.get("lean_verdict"),
            "lean_comment": a.meta.get("lean_comment"),
            "created": a.meta.get("created") or a.meta.get("date"),
            "updated": a.meta.get("updated") or a.meta.get("date")}


def _index(g: Graph):
    nodes = {n.id: n for n in g.nodes()}
    formalizes: dict[str, list[str]] = {}
    deps: dict[str, list[tuple[str, str]]] = {}
    for e in g.edges():
        if e.type == "formalizes":
            formalizes.setdefault(e.source, []).append(e.target)
        elif e.hard:
            deps.setdefault(e.source, []).append((e.target, e.type))
    return nodes, formalizes, deps


def _clip(code: str, n: int = 60) -> str:
    lines = code.split("\n")
    return code if len(lines) <= n else "\n".join(lines[:n]) + "\n  -- … (truncated)"


def _entry(n, formalizes, deps, nodes, g) -> dict:
    lean = [{"name": nodes[l].meta.get("decl"), "status": nodes[l].meta.get("lean_status"),
             "file": nodes[l].meta.get("file"), "code": _clip(nodes[l].content)}
            for l in formalizes.get(n.id, []) if l in nodes]
    dep = [{"id": t, "title": nodes[t].title, "label": nodes[t].meta.get("label"), "type": ty}
           for t, ty in deps.get(n.id, []) if t in nodes]
    reviews, comments = g.attachments(n.id, "review"), g.attachments(n.id, "comment")
    last = reviews[-1] if reviews else None
    return {
        "id": n.id, "label": n.meta.get("label"), "title": n.title,
        "chapter": n.meta.get("chapter"), "kind": n.meta.get("content_type") or "statement",
        # granularity axis (authored/AI wins; _assign_groups_levels fills the rest)
        "group": n.meta.get("group"), "level": n.meta.get("level"),
        "ref": n.meta.get("ref"),      # source-book provenance (\dcref{…})
        "body": re.sub(r"(?<!\\)%.*", "", n.content),
        "lean_status": n.meta.get("lean_status") or "empty",
        "mathlib_name": n.meta.get("mathlib_name"), "status": n.meta.get("status"),
        "tags": n.meta.get("tags"), "lean": lean, "deps": dep,
        "reviewed": bool(reviews),
        "maths_verdict": (last.meta.get("maths_verdict") if last else None),
        "lean_verdict": (last.meta.get("lean_verdict") if last else None),
        "reviews": [_review_att(r) for r in reviews], "comments": [_att(c) for c in comments],
    }


_MED_KINDS = {"theorem", "proposition", "lemma"}


def _assign_groups_levels(entries: list) -> None:
    """Attach a ``group`` (cluster id) and ``level`` (``coarse|medium|fine``) to
    every entry, in place. Values authored on the node (or written by a future
    ``hgraph extract`` AI pass) always win; anything missing gets a **heuristic
    stub** so the graph's group-collapse + level filter have something to render.

    The stub is deliberately cheap and self-contained (no deps): ``group`` by
    label-propagation community detection over the hard-dependency graph (falling
    back to chapter when that degenerates), ``level`` by how foundational a node is
    (``coarse`` = the most-depended-on ~12%; ``medium`` = a main-result kind;
    ``fine`` = the rest). It is a placeholder for AI-assigned values, not a claim
    of good clustering. Because it is computed at build time from the synced graph
    (not persisted to node files), it survives ``build.sh``'s wipe-and-resync.
    """
    n = len(entries)
    if not n:
        return
    idx = {e["id"]: i for i, e in enumerate(entries)}
    adj: list[list[int]] = [[] for _ in range(n)]
    usedby = [0] * n
    for i, e in enumerate(entries):
        for d in (e.get("deps") or []):
            j = idx.get(d["id"])
            if j is None or j == i:
                continue
            adj[i].append(j)
            adj[j].append(i)
            usedby[j] += 1                       # e depends on d ⇒ d is used by e

    # ── group: single-level Louvain (modularity local-moving), deterministic. ──
    # More stable than label propagation, whose ΔQ-free updates tend to collapse a
    # dense dep graph into one monster community. The modularity term penalises that
    # (it grows with Σtot), and an adaptive resolution guarantees a usable split.
    deg = [len(a) for a in adj]
    two_m = sum(deg) or 1

    def louvain(gamma: float) -> list[int]:
        comm = list(range(n))
        sigma = deg[:]                                    # Σtot (degree sum) per community
        for _ in range(20):
            moved = False
            for i in range(n):
                if not adj[i]:
                    continue
                ci = comm[i]
                sigma[ci] -= deg[i]
                nw: dict[int, int] = {}
                for j in adj[i]:
                    nw[comm[j]] = nw.get(comm[j], 0) + 1
                best_c = ci
                best_gain = nw.get(ci, 0) - gamma * deg[i] * sigma[ci] / two_m
                for c, wic in nw.items():
                    if c == ci:
                        continue
                    gain = wic - gamma * deg[i] * sigma[c] / two_m
                    if gain > best_gain + 1e-12 or (abs(gain - best_gain) <= 1e-12 and c < best_c):
                        best_gain, best_c = gain, c
                comm[i] = best_c
                sigma[best_c] += deg[i]
                if best_c != ci:
                    moved = True
            if not moved:
                break
        return comm

    def spread(cm: list[int]) -> tuple[float, int]:
        sz: dict[int, int] = {}
        for i in range(n):
            if adj[i]:
                sz[cm[i]] = sz.get(cm[i], 0) + 1
        tot = sum(sz.values())
        return ((max(sz.values()) / tot) if tot else 1.0), len(sz)

    comm = louvain(1.0)
    for gamma in (1.6, 2.6, 4.0):                          # raise resolution if one blob dominates
        frac, k = spread(comm)
        if frac <= 0.4 and k >= 3:
            break
        comm = louvain(gamma)

    # isolated nodes carry no signal ⇒ bucket them by chapter; connected keep community
    gid = ["ch:" + (entries[i].get("chapter") or "·") if not adj[i] else "c%d" % comm[i]
           for i in range(n)]
    # absorb tiny communities into the neighbouring group they touch most
    MIN = 5
    for _ in range(3):
        gsz: dict[str, int] = {}
        for x in gid:
            gsz[x] = gsz.get(x, 0) + 1
        moved = False
        for i in range(n):
            if not adj[i] or gsz.get(gid[i], 0) >= MIN:
                continue
            cnt: dict[str, int] = {}
            for j in adj[i]:
                if gid[j] != gid[i]:
                    cnt[gid[j]] = cnt.get(gid[j], 0) + 1
            if cnt:
                gid[i] = min(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0]
                moved = True
        if not moved:
            break

    # cap the count so the group overview stays readable: repeatedly fold the
    # smallest community that still touches another community into that neighbour.
    CAP = 30
    for _ in range(n):
        csz: dict[str, int] = {}
        for i in range(n):
            if adj[i]:
                csz[gid[i]] = csz.get(gid[i], 0) + 1
        if len(csz) <= CAP:
            break
        merged = False
        for small, _sz in sorted(csz.items(), key=lambda kv: (kv[1], kv[0])):
            w: dict[str, int] = {}
            for i in range(n):
                if gid[i] != small:
                    continue
                for j in adj[i]:
                    if gid[j] != small:
                        w[gid[j]] = w.get(gid[j], 0) + 1
            if not w:
                continue                           # island: try the next-smallest
            tgt = max(w.items(), key=lambda kv: (kv[1], kv[0]))[0]
            for i in range(n):
                if gid[i] == small:
                    gid[i] = tgt
            merged = True
            break
        if not merged:
            break

    def stub_group(i: int) -> str:
        return gid[i]

    # ── level: coarse = most-depended-on ~12%; medium = main-result kind; else fine
    order = sorted(range(n), key=lambda i: usedby[i], reverse=True)
    coarse = set(order[:max(1, round(n * 0.12))])

    def stub_level(i: int) -> str:
        if i in coarse and usedby[i] > 0:
            return "coarse"
        if (entries[i].get("kind") or "") in _MED_KINDS:
            return "medium"
        return "fine"

    for i, e in enumerate(entries):
        if not e.get("group"):
            e["group"] = stub_group(i)
        if e.get("level") not in ("coarse", "medium", "fine"):
            e["level"] = stub_level(i)


def collect(g: Graph, *, title: str = "Blueprint") -> dict:
    nodes, formalizes, deps = _index(g)
    order = {n.id: n.meta.get("order") for n in nodes.values()}
    entries = [_entry(n, formalizes, deps, nodes, g)
               for n in nodes.values() if n.meta.get("generated") == "blueprint"]
    # document order first (sync stamps `order` on every blueprint node);
    # alphabetical is only the fallback for hand-added nodes without one
    entries.sort(key=lambda e: (e.get("chapter") or "",
                                order.get(e["id"]) if order.get(e["id"]) is not None else 10**9,
                                e["title"] or ""))
    _assign_groups_levels(entries)
    return {"title": title, "entries": entries}


def collect_one(g: Graph, nid: str) -> dict:
    nodes, formalizes, deps = _index(g)
    return _entry(nodes[nid], formalizes, deps, nodes, g)


_ABBR = {"definition": "Def", "lemma": "Lem", "theorem": "Thm", "proposition": "Prop",
         "corollary": "Cor", "remark": "Rmk", "example": "Ex", "conjecture": "Conj",
         "claim": "Claim", "fact": "Fact"}


def build_document(g: Graph, blueprint: str | Path, *, title: str) -> dict:
    """The full blueprint document, numbered, + a by-id map of enriched statements,
    a label→number cross-reference table, and a statement→chapter location map."""
    nodes, formalizes, deps = _index(g)
    entries = {n.meta.get("label"): _entry(n, formalizes, deps, nodes, g)
               for n in nodes.values() if n.meta.get("generated") == "blueprint"}
    # assign group/level stubs BEFORE the chapter walk snapshots b["enrich"]
    # below — assigning after left enrich.group null on every entry without
    # an authored group, so the doc view's group tags silently vanished
    _assign_groups_levels(list(entries.values()))
    chapters = parse_document(read_blueprint(blueprint))
    by_id, refs, loc = {}, {}, {}
    keys = ("lean_status", "mathlib_name", "reviewed", "maths_verdict", "lean_verdict",
            "lean", "deps", "reviews", "comments", "status", "tags", "ref", "group")
    for ci, ch in enumerate(chapters, 1):
        ch["num"] = str(ci)
        sec = {2: 0, 3: 0, 4: 0}
        cnt = 0
        for b in ch["blocks"]:
            if b["t"] == "head" and b["level"] <= 4:
                lvl = b["level"]
                sec[lvl] += 1
                for d in (3, 4):
                    if d > lvl:
                        sec[d] = 0
                b["num"] = "%d.%s" % (ci, ".".join(str(sec[l]) for l in range(2, lvl + 1)))
            elif b["t"] == "stmt":
                cnt += 1
                b["num"] = f"{ci}.{cnt}"
                b["abbr"] = _ABBR.get(b["content_type"], b["content_type"].title())
                lbl = b.get("label")
                e = entries.get(lbl) if lbl else None
                if e:
                    b["id"] = e["id"]
                    b["enrich"] = {k: e[k] for k in keys}
                    by_id[e["id"]] = e
                    loc[e["id"]] = ci - 1
                # register every \label alias (canonical + legacy book labels) so
                # a \ref{} to any of them resolves, not just the canonical one
                for alias in (b.get("labels") or ([lbl] if lbl else [])):
                    refs[alias] = {"num": b["num"], "id": (e["id"] if e else None), "abbr": b["abbr"]}
    graph_entries = list(by_id.values())   # groups/levels already assigned above
    return {"title": title, "mode": "doc", "chapters": chapters,
            "entries": graph_entries, "refs": refs, "loc": loc}


def _resolve_blueprint(blueprint, root):
    if blueprint and Path(blueprint).exists():
        return blueprint
    bp = load_config(root).get("blueprint")
    return bp if bp and Path(bp).exists() else None


def _normalize_repo(repo):
    """``owner/name``, the only spelling the review link can use: the frontend
    builds ``https://github.com/<repo>/issues/new`` from it, so a full URL here
    would silently produce a doubled, broken link. A URL is the natural thing to
    write, though, so accept one and reduce it rather than fail."""
    if not repo:
        return None
    s = str(repo).strip().rstrip("/")
    s = re.sub(r"^(?:https?://|git@)(?:www\.)?github\.com[:/]", "", s)
    s = re.sub(r"\.git$", "", s)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", s):
        print(f"warning: repo: {repo!r} is not `owner/name` (nor a GitHub URL) — "
              f"review links disabled for this project")
        return None
    return s


def _resolve_repo(repo, root):
    """``owner/name`` for the GitHub-issue review link, from ``--repo`` / the
    manifest entry, else the project's ``hgraph/config.yaml`` -> ``site.repo``."""
    if repo:
        return _normalize_repo(repo)
    return _normalize_repo((load_config(root).get("site") or {}).get("repo"))


def project_data(g: Graph, *, title: str, blueprint=None, macros_from=None,
                 root: str = ".", repo=None) -> dict:
    """One project's full data payload — everything the React `ProjectView`
    needs: the numbered document (chapters/prose/cross-refs) if a blueprint
    is configured, else a flat statement list; entries (statements + Lean +
    deps + reviews/comments); bibliography; macros for client-side KaTeX; a
    precomputed Graphviz layout for the dependency graph; the closure/
    frontier analysis for the summary tab; and the GitHub-issue repo (if
    configured). Written to ``<root>/data.json`` by `hgraph site`, or served
    live at ``GET /<root>/data.json`` by `hgraph serve`. Closure/frontier
    analysis (``hgraph.analysis.Analysis``) is deliberately not embedded here:
    it runs over the *whole* graph (including Lean-only nodes with no
    blueprint label), which would silently disagree with a summary computed
    over just the documented entries — see the "Blueprint summary" tab,
    which instead recomputes closure client-side over `entries` alone, the
    same way `hgraph frontier`/`hgraph stats` compute it independently via
    their own `Analysis(g)` call."""
    from .layout import render_svgs

    bp = _resolve_blueprint(blueprint, root)
    data = build_document(g, bp, title=title) if bp else {**collect(g, title=title), "mode": "list"}
    ta = discover_titleauthor(bp) if bp else {}
    data.update({
        "bib": discover_bib(bp) if bp else [],
        "docTitle": ta.get("title") or title,
        "docAuthor": ta.get("author"),
        "macros": resolve_macros(bp, macros_from),
        "repo": _resolve_repo(repo, root),
        "gvsvg": render_svgs(data),
    })
    return data


# --------------------------------------------------------------------------- #
# LaTeX macro extraction
# --------------------------------------------------------------------------- #
def _balanced(text: str, start: int):
    depth = 0
    for k in range(start, len(text)):
        if text[k] == "{":
            depth += 1
        elif text[k] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:k]
    return None


def extract_macros(sty_text: str) -> dict:
    """LaTeX accepts the macro name both braced (``\\newcommand{\\foo}``) and
    bare (``\\newcommand\\foo``) — real blueprints use both freely, and a
    missed definition means KaTeX later shows the raw macro in the page."""
    macros: dict[str, str] = {}
    for m in re.finditer(
            r"\\DeclareMathOperator(\*?)\s*(?:\{\\([A-Za-z]+)\}|\\([A-Za-z]+))\s*\{([^}]*)\}",
            sty_text):
        name = m.group(2) or m.group(3)
        macros["\\" + name] = "\\operatorname%s{%s}" % (m.group(1), m.group(4))
    for m in re.finditer(
            r"\\(?:new|renew|provide)command\*?\s*(?:\{\\([A-Za-z]+)\}|\\([A-Za-z]+))"
            r"\s*(?:\[\d+\]\s*(?:\[[^\]]*\])?)?",
            sty_text):
        name = m.group(1) or m.group(2)
        brace = sty_text.find("{", m.end())
        body = _balanced(sty_text, brace) if brace != -1 else None
        if body is not None and "\\lean" not in body:
            macros.setdefault("\\" + name, body)
    for m in re.finditer(r"\\def\s*\\([A-Za-z]+)\s*(?:#\d)*\s*\{", sty_text):
        body = _balanced(sty_text, m.end() - 1)
        if body is not None and "\\lean" not in body:
            macros.setdefault("\\" + m.group(1), body)
    return macros


def discover_macros(blueprint) -> dict:
    """Auto-discover the project's LaTeX macros (\\Spec, \\Pic, …) by scanning the
    blueprint's own directory tree for ``\\newcommand`` / ``\\DeclareMathOperator``
    / ``\\def`` in any ``.sty`` / ``.tex`` — so KaTeX renders them with no config."""
    macros: dict[str, str] = {}
    root = Path(blueprint).parent if blueprint else None
    if not root or not root.exists():
        return macros
    for f in sorted(list(root.rglob("*.sty")) + list(root.rglob("*.tex"))):
        try:
            for k, v in extract_macros(f.read_text(encoding="utf-8")).items():
                macros.setdefault(k, v)
        except Exception:
            pass
    return macros


def parse_bib(text: str) -> list:
    """Very small BibTeX reader — enough to list entries in the dashboard's
    Bibliography view (key, title, author, year, venue, url)."""
    out = []
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,(.*?)\n\s*\}", text, re.S):
        typ, key, body = m.group(1).lower(), m.group(2).strip(), m.group(3)
        if typ in ("comment", "string", "preamble"):
            continue
        f = {}
        for fm in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\"|[^,\n]+)", body):
            # peel exactly one delimiter layer — strip('{}"') eats ALL leading/
            # trailing braces, mangling values that end in a braced group like
            # "The {ABC} Conjecture" or protected capitals "{H}odge"
            v = fm.group(2).strip()
            if len(v) >= 2 and ((v[0] == "{" and v[-1] == "}")
                                or (v[0] == '"' and v[-1] == '"')):
                v = v[1:-1]
            f[fm.group(1).lower()] = re.sub(r"\s+", " ", v.strip())
        out.append({"key": key, "type": typ, "title": f.get("title"),
                    "author": f.get("author"), "year": f.get("year"),
                    "journal": f.get("journal"), "booktitle": f.get("booktitle"),
                    "publisher": f.get("publisher"), "volume": f.get("volume"),
                    "number": f.get("number"), "pages": f.get("pages"),
                    "url": f.get("url") or (("https://doi.org/" + f["doi"]) if f.get("doi") else None)})
    return out


def discover_titleauthor(blueprint) -> dict:
    """Find the blueprint's ``\\title{…}`` / ``\\author{…}`` (they usually live in
    the print/web entry .tex, not the content file) so the dashboard can show a
    title page. Brace-balanced so nested macros aren't cut off."""
    root = Path(blueprint).parent if blueprint else None
    out: dict = {}
    if not root or not root.exists():
        return out
    for f in sorted(root.rglob("*.tex")):
        if len(out) == 2:
            break
        try:
            t = f.read_text(encoding="utf-8")
        except Exception:
            continue
        for key in ("title", "author"):
            if key in out:
                continue
            m = re.search(r"\\%s\s*\{" % key, t)
            if m:
                body = _balanced(t, t.index("{", m.start()))
                if body:
                    out[key] = re.sub(r"\s+", " ", body).strip()
    return out


def discover_bib(blueprint) -> list:
    """Parse every ``.bib`` in the blueprint's directory tree (deduped by key)."""
    root = Path(blueprint).parent if blueprint else None
    if not root or not root.exists():
        return []
    out, seen = [], set()
    for f in sorted(root.rglob("*.bib")):
        try:
            for e in parse_bib(f.read_text(encoding="utf-8")):
                if e["key"] not in seen:
                    seen.add(e["key"])
                    out.append(e)
        except Exception:
            pass
    return out


def _macros(macros_from):
    if macros_from and Path(macros_from).exists():
        return extract_macros(Path(macros_from).read_text(encoding="utf-8"))
    return {}


def resolve_macros(blueprint, macros_from) -> dict:
    m = discover_macros(blueprint)
    m.update(_macros(macros_from))            # an explicit --macros wins
    return m
