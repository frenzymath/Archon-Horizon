"""Blueprint data — the full blueprint document as JSON, enriched with the graph.

Pure data, no rendering: numbered chapters, statements enriched with the
graph's data (`lean_status`, Lean declarations, dependencies, reviews,
comments). The React frontend (`frontend/`, shipped pre-built at
`hgraph/webui/`) does all the rendering — this module only ever produces
JSON, written by `hgraph site` or served live by `hgraph serve`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from archon_horizon.blueprint.checks import is_countable

from .graph import Graph
from .sync import DISPLAY_ENVS, load_config, parse_document, read_blueprint


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
           for t, ty in deps.get(n.id, [])
           if t in nodes and is_countable({"type": nodes[t].meta.get("content_type")})]
    reviews, comments = g.attachments(n.id, "review"), g.attachments(n.id, "comment")
    last = reviews[-1] if reviews else None
    return {
        "id": n.id, "label": n.meta.get("label"), "title": n.title,
        "chapter": n.meta.get("chapter"), "kind": n.meta.get("content_type") or "statement",
        # Horizon still exposes semantic groups in its graph UI. Upstream hgraph
        # now only consumes levels, so grouping remains a local compatibility field.
        "group": n.meta.get("group"), "level": n.meta.get("level"),
        "ref": n.meta.get("ref"),      # source-book provenance (\dcref{…})
        "sketch": bool(n.meta.get("sketch")),   # \sketch — proof deliberately incomplete
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


def _assign_levels(entries: list) -> None:
    """Attach a ``level`` (``coarse|medium|fine``) to every entry, in place.

    A value authored on the node (or written by a future ``hgraph extract`` AI
    pass) always wins; anything missing gets a **heuristic stub** so the graph's
    level filter has something to render: ``coarse`` = the most-depended-on ~12%,
    ``medium`` = a main-result kind, ``fine`` = the rest. Because it is computed
    at build time from the synced graph (not persisted to node files), it
    survives ``build.sh``'s wipe-and-resync.
    """
    n = len(entries)
    if not n:
        return
    idx = {e["id"]: i for i, e in enumerate(entries)}
    usedby = [0] * n
    for i, e in enumerate(entries):
        for d in (e.get("deps") or []):
            j = idx.get(d["id"])
            if j is not None and j != i:
                usedby[j] += 1                   # e depends on d ⇒ d is used by e

    order = sorted(range(n), key=lambda i: usedby[i], reverse=True)
    coarse = set(order[:max(1, round(n * 0.12))])

    def stub_level(i: int) -> str:
        if i in coarse and usedby[i] > 0:
            return "coarse"
        if (entries[i].get("kind") or "") in _MED_KINDS:
            return "medium"
        return "fine"

    for i, e in enumerate(entries):
        if e.get("level") not in ("coarse", "medium", "fine"):
            e["level"] = stub_level(i)


def _assign_groups_levels(entries: list) -> None:
    """Assign upstream levels plus Horizon's retained semantic graph groups.

    Authored groups win. Missing groups use the deterministic, single-level
    Louvain heuristic that Horizon already exposed before upstream hgraph
    retired group rendering from its standalone frontend.
    """
    _assign_levels(entries)
    n = len(entries)
    if not n:
        return
    idx = {e["id"]: i for i, e in enumerate(entries)}
    adj: list[list[int]] = [[] for _ in range(n)]
    for i, entry in enumerate(entries):
        for dependency in entry.get("deps") or []:
            j = idx.get(dependency["id"])
            if j is None or j == i:
                continue
            adj[i].append(j)
            adj[j].append(i)

    degree = [len(neighbours) for neighbours in adj]
    two_m = sum(degree) or 1

    def louvain(gamma: float) -> list[int]:
        community = list(range(n))
        sigma = degree[:]
        for _ in range(20):
            moved = False
            for i in range(n):
                if not adj[i]:
                    continue
                current = community[i]
                sigma[current] -= degree[i]
                neighbour_weights: dict[int, int] = {}
                for j in adj[i]:
                    neighbour = community[j]
                    neighbour_weights[neighbour] = neighbour_weights.get(neighbour, 0) + 1
                best = current
                best_gain = (neighbour_weights.get(current, 0)
                             - gamma * degree[i] * sigma[current] / two_m)
                for candidate, weight in neighbour_weights.items():
                    if candidate == current:
                        continue
                    gain = weight - gamma * degree[i] * sigma[candidate] / two_m
                    if (gain > best_gain + 1e-12
                            or (abs(gain - best_gain) <= 1e-12 and candidate < best)):
                        best_gain, best = gain, candidate
                community[i] = best
                sigma[best] += degree[i]
                moved |= best != current
            if not moved:
                break
        return community

    def spread(community: list[int]) -> tuple[float, int]:
        sizes: dict[int, int] = {}
        for i in range(n):
            if adj[i]:
                sizes[community[i]] = sizes.get(community[i], 0) + 1
        total = sum(sizes.values())
        return ((max(sizes.values()) / total) if total else 1.0), len(sizes)

    community = louvain(1.0)
    for gamma in (1.6, 2.6, 4.0):
        largest, count = spread(community)
        if largest <= 0.4 and count >= 3:
            break
        community = louvain(gamma)

    group_ids = [
        "ch:" + (entries[i].get("chapter") or "·") if not adj[i]
        else f"c{community[i]}"
        for i in range(n)
    ]

    # Fold tiny communities into the neighbouring group they touch most.
    for _ in range(3):
        sizes: dict[str, int] = {}
        for group_id in group_ids:
            sizes[group_id] = sizes.get(group_id, 0) + 1
        moved = False
        for i in range(n):
            if not adj[i] or sizes.get(group_ids[i], 0) >= 5:
                continue
            neighbours: dict[str, int] = {}
            for j in adj[i]:
                if group_ids[j] != group_ids[i]:
                    neighbours[group_ids[j]] = neighbours.get(group_ids[j], 0) + 1
            if neighbours:
                group_ids[i] = min(neighbours.items(), key=lambda item: (-item[1], item[0]))[0]
                moved = True
        if not moved:
            break

    # Keep the overview readable by capping connected communities at 30.
    for _ in range(n):
        sizes: dict[str, int] = {}
        for i in range(n):
            if adj[i]:
                sizes[group_ids[i]] = sizes.get(group_ids[i], 0) + 1
        if len(sizes) <= 30:
            break
        merged = False
        for smallest, _ in sorted(sizes.items(), key=lambda item: (item[1], item[0])):
            weights: dict[str, int] = {}
            for i in range(n):
                if group_ids[i] != smallest:
                    continue
                for j in adj[i]:
                    if group_ids[j] != smallest:
                        weights[group_ids[j]] = weights.get(group_ids[j], 0) + 1
            if not weights:
                continue
            target = max(weights.items(), key=lambda item: (item[1], item[0]))[0]
            for i in range(n):
                if group_ids[i] == smallest:
                    group_ids[i] = target
            merged = True
            break
        if not merged:
            break

    for i, entry in enumerate(entries):
        if not entry.get("group"):
            entry["group"] = group_ids[i]


def collect(g: Graph, *, title: str = "Blueprint") -> dict:
    nodes, formalizes, deps = _index(g)
    order = {n.id: n.meta.get("order") for n in nodes.values()}
    entries = [_entry(n, formalizes, deps, nodes, g)
               for n in nodes.values()
               if n.meta.get("generated") == "blueprint"
               and is_countable({"type": n.meta.get("content_type")})]
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


def _chapter_label(n: int, lettered: bool) -> str:
    """``1, 2, 3 …`` normally; ``A, B, … Z, AA`` after ``\\appendix``."""
    if not lettered:
        return str(n)
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(ord("A") + r) + out
    return out


def _join_num(prefix: str | None, *parts: str) -> str:
    """``2.3`` under a numbered chapter, ``A.3`` under an appendix — and just
    ``3`` under a ``\\chapter*``, which has no number to hang it off."""
    return ".".join(([prefix] if prefix else []) + list(parts))


# Display-math environments LaTeX numbers; the starred forms it does not, which
# is why the pattern refuses a `*`.
_EQ_RE = re.compile(r"\\begin\{(" + "|".join(DISPLAY_ENVS[:-1]) + r")\}(.*?)\\end\{\1\}",
                    re.DOTALL)
_ONE_ROW = ("equation", "multline")          # one number for the whole body
_ROW_SEP = re.compile(r"(\\\\(?:\s*\[[^\]]*\])?)")
_NESTED_ENV = re.compile(r"\\begin\{([a-zA-Z]+\*?)\}.*?\\end\{\1\}", re.DOTALL)
_TAG_RE = re.compile(r"\\tag\*?\{([^{}]*)\}")


def _number_equations(tex: str, prefix: str | None, count: int, ci: int,
                      refs: dict) -> tuple[str, int]:
    """Number the display equations in ``tex`` and register their ``\\label``\\s.

    The number is written back into the TeX as ``\\tag{…}`` rather than rendered
    beside it: KaTeX puts a ``\\tag`` exactly where LaTeX puts an equation
    number — including once per row inside ``align`` — so the number the reader
    sees and the one ``\\cref`` resolves to cannot drift apart. An author's own
    ``\\tag`` wins and consumes no number, as in LaTeX; ``\\nonumber``/``\\notag``
    rows are skipped. Returns the rewritten TeX and the new counter.
    """
    def one_env(m: "re.Match") -> str:
        nonlocal count
        env, body = m.group(1), m.group(2)
        hidden: list[str] = []

        def hide(h: "re.Match") -> str:
            hidden.append(h.group(0))
            return "\x00%d\x00" % (len(hidden) - 1)

        # a nested {cases}/{matrix} has \\ row breaks of its own — not ours
        masked = body if env in _ONE_ROW else _NESTED_ENV.sub(hide, body)
        parts = [masked] if env in _ONE_ROW else _ROW_SEP.split(masked)
        for i in range(0, len(parts), 2):      # odd indices are the separators
            row = parts[i]
            if not row.strip() or re.search(r"\\(?:nonumber|notag)\b", row):
                continue
            tag = _TAG_RE.search(row)
            if tag:
                num = tag.group(1)
            else:
                count += 1
                num = _join_num(prefix, str(count))
                parts[i] = row = row.rstrip() + "\\tag{%s}" % num
            # only a plain number makes a usable element id — an author's
            # `\tag{$\star$}` still resolves, it just lands on the chapter
            anchor = "eq-" + num if re.fullmatch(r"[\w.]+", num) else ""
            for lbl in re.findall(r"\\label\{([^}]*)\}", row):
                refs[lbl.strip()] = {"num": num, "id": None, "abbr": "Eq.",
                                     "kind": "eq", "ch": ci, "anchor": anchor}
        out = "".join(parts)
        out = re.sub(r"\x00(\d+)\x00", lambda h: hidden[int(h.group(1))], out)
        return "\\begin{%s}%s\\end{%s}" % (env, out, env)

    return _EQ_RE.sub(one_env, tex), count


def number_document(chapters: list[dict]) -> dict:
    """Stamp ``num`` on every chapter, heading, statement and display equation,
    in place, and return the label → cross-reference table for everything that
    is *not* a statement (chapters, sections, equations) — what ``\\cref`` needs
    to resolve beyond the theorem-like environments the graph knows about.

    LaTeX's own numbering controls are honoured, so a blueprint that reads
    correctly when compiled reads correctly here:

    * ``\\chapter*{…}`` / ``\\section*{…}`` take no number and do not advance the
      counter — their statements simply drop the chapter prefix (``1``, ``2``).
    * ``\\appendix`` restarts the chapter counter in letters: ``A``, ``B``, … so
      the appendices stop reading as chapters 13, 14, 15.
    """
    refs: dict = {}
    counter, lettered = 0, False
    for ci, ch in enumerate(chapters):
        if ch.get("appendix") and not lettered:
            lettered, counter = True, 0
        if ch.get("starred"):
            ch["num"] = None
        else:
            counter += 1
            ch["num"] = _chapter_label(counter, lettered)
        for lbl in ch.get("labels") or ():
            refs[lbl] = {"num": ch["num"], "id": None,
                         "abbr": "Appendix" if ch.get("appendix") else "Chapter",
                         "kind": "chap", "ch": ci, "anchor": ""}
        sec = {2: 0, 3: 0, 4: 0}
        cnt = 0
        eqs = 0                        # equations are numbered per chapter, as in LaTeX
        for b in ch["blocks"]:
            if b["t"] == "head" and b["level"] <= 4:
                if b.get("starred"):
                    b["num"] = None
                    continue
                lvl = b["level"]
                sec[lvl] += 1
                for d in (3, 4):
                    if d > lvl:
                        sec[d] = 0
                b["num"] = _join_num(ch["num"], *(str(sec[l]) for l in range(2, lvl + 1)))
                for lbl in b.get("labels") or ():
                    refs[lbl] = {"num": b["num"], "id": None, "abbr": "Section",
                                 "kind": "sec", "ch": ci, "anchor": "sec-" + b["num"]}
                continue
            if b["t"] == "stmt":
                cnt += 1
                b["num"] = _join_num(ch["num"], str(cnt))
                b["abbr"] = _ABBR.get(b["content_type"], b["content_type"].title())
            key = "body" if b["t"] == "stmt" else "tex"
            if b.get(key):
                b[key], eqs = _number_equations(b[key], ch["num"], eqs, ci, refs)
    return refs


def build_document(g: Graph, blueprint: str | Path, *, title: str) -> dict:
    """The full blueprint document, numbered, + a by-id map of enriched statements,
    a label→number cross-reference table, and a statement→chapter location map."""
    nodes, formalizes, deps = _index(g)
    entries = {
        n.meta.get("label"): _entry(n, formalizes, deps, nodes, g)
        for n in nodes.values()
        if n.meta.get("generated") == "blueprint"
        and is_countable({"type": n.meta.get("content_type")})
    }
    # Assign Horizon's group plus upstream's level stubs before snapshotting enrich.
    _assign_groups_levels(list(entries.values()))
    chapters = parse_document(read_blueprint(blueprint))
    by_id, refs, loc = {}, {}, {}
    keys = ("lean_status", "mathlib_name", "reviewed", "maths_verdict", "lean_verdict",
            "lean", "deps", "reviews", "comments", "status", "tags", "ref", "sketch",
            "group")
    # chapter/section/equation cross-references first; the statements below
    # overwrite any label they share (a statement is the more useful target)
    refs.update(number_document(chapters))
    for ci, ch in enumerate(chapters):
        for b in ch["blocks"]:
            if b["t"] == "stmt":
                lbl = b.get("label")
                e = entries.get(lbl) if lbl else None
                if e:
                    b["id"] = e["id"]
                    b["enrich"] = {k: e[k] for k in keys}
                    by_id[e["id"]] = e
                    loc[e["id"]] = ci
                # register every \label alias (canonical + legacy book labels) so
                # a \ref{} to any of them resolves, not just the canonical one
                for alias in (b.get("labels") or ([lbl] if lbl else [])):
                    refs[alias] = {"num": b["num"], "id": (e["id"] if e else None),
                                   "abbr": b["abbr"], "kind": "stmt", "ch": ci,
                                   "anchor": ("stmt-" + e["id"]) if e else ""}
    graph_entries = list(by_id.values())   # groups/levels already assigned above
    return {"title": title, "mode": "doc", "chapters": chapters,
            "entries": graph_entries, "refs": refs, "loc": loc}


# `\citeext{Handle}{label}` (cross-project cite) or bare `\citeext{Handle}`. The
# second brace group is optional; unlike the leanblueprint macros it survives
# into the body verbatim and is rendered client-side (see frontend/src/latex.ts),
# exactly like `\ref`/`\cite`. Here we only pre-resolve the *target numbers*,
# which live in another project and so cannot be looked up in the browser.
_CITEEXT_RE = re.compile(r"\\citeext\{([^{}]*)\}(?:\{([^{}]*)\})?")


def resolve_extrefs(chapters, index: dict) -> dict:
    """Resolve every ``\\citeext{handle}{label}`` in a document's prose against a
    cross-project ``index`` (``{handle: {"root", "name", "refs"}}``), returning
    the ``extrefs`` payload the frontend renders from::

        {handle: {"root", "name", "refs": {label: {"num", "abbr"}}}}

    Only the handles and labels actually cited are included, so the map a project
    ships stays small. An unknown handle is skipped (the frontend then renders the
    citation as muted text); a known handle with an unresolved label still ships
    its ``root``/``name`` so the link works, just without a number."""
    out: dict = {}
    for ch in chapters or []:
        for b in ch.get("blocks", []):
            text = " ".join(s for s in (b.get("tex"), b.get("body"), b.get("title")) if s)
            for m in _CITEEXT_RE.finditer(text):
                handle = (m.group(1) or "").strip()
                label = (m.group(2) or "").strip()
                proj = index.get(handle)
                if not proj:
                    continue
                entry = out.setdefault(
                    handle, {"root": proj["root"], "name": proj["name"], "refs": {}})
                if label:
                    r = (proj.get("refs") or {}).get(label)
                    if r:
                        entry["refs"][label] = {"num": r["num"], "abbr": r["abbr"]}
    return out


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
        # stderr, not stdout: callers of this module render payloads to stdout,
        # and a warning mixed into that would corrupt machine-readable output.
        print(f"warning: repo: {repo!r} is not `owner/name` (nor a GitHub URL) — "
              f"review links disabled for this project", file=sys.stderr)
        return None
    return s


def _resolve_repo(repo, root):
    """``owner/name`` for the GitHub-issue review link, from ``--repo`` / the
    manifest entry, else the project's ``hgraph/config.yaml`` -> ``site.repo``."""
    if repo:
        return _normalize_repo(repo)
    return _normalize_repo((load_config(root).get("site") or {}).get("repo"))


def project_data(g: Graph, *, title: str, blueprint=None, macros_from=None,
                 root: str = ".", repo=None, theme: dict | None = None) -> dict:
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
        "theme": theme,
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
