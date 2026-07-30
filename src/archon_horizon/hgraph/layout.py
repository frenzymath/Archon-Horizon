"""Build-time Graphviz layout.

The graph modal's collapsed chapter overview is otherwise laid out **in the
browser** by Graphviz-compiled-to-WASM (``@viz-js/viz``, in a Web Worker) the
first time the modal opens. This module builds the same overview DOT the JS
builds (``dotMixed`` with nothing expanded, in ``frontend/src/graphDot.ts``),
runs the system ``dot`` once at build time, and returns positioned SVG. The
page embeds that SVG for an instant first paint and skips the WASM layout.

It is a **pure optimisation**: if ``dot`` isn't on ``PATH`` (or fails), this
returns ``{}`` and the modal lays the overview out live with the bundled WASM
engine instead. Nothing here is required at runtime.

INVARIANT: the styling here (border = statement status, fill = proof status,
rect = definition / ellipse = theorem, the purple chapter boxes) must stay in
lockstep with ``frontend/src/graphDot.ts`` + ``frontend/src/palette.ts``.
Both sides are small; keep them mirrored.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess

# --- style maps: mirror of GRAPH in frontend/src/palette.ts, which is the
# source of truth for the web UI's graph. Keep the two in lockstep. ------------
_DEF_KINDS = {"definition", "example", "remark", "notation", "convention"}
_GBORDER = {"formalized": "#2e7d32", "ready": "#1565c0", "blocked": "#b0bec5"}
_GFILL = {"done": "#66bb6a", "local": "#c8e6c9", "incomplete": "#ffcc80",
          "ready": "#bbdefb", "notready": "#eef1f4"}
_CLUSTER = {"fill": "#ede9fe", "border": "#7c3aed", "text": "#3b0a91"}
# a semantic-group cluster is neutral: the green ramp on its box carries the
# progress signal, so the frame stays out of the way
_GROUP_CLUSTER = {"fill": "#f4f5f9", "border": "#c9cfda", "text": "#5b6470"}
_NODE_TEXT = "#1c2024"
_EDGE = "#8a93a0"
_DONE = {"lean_ok", "mathlib_ok"}


def dot_available() -> bool:
    """True iff the Graphviz ``dot`` binary is on PATH."""
    return shutil.which("dot") is not None


def _plain_tex(s: str) -> str:
    """Mirror of the JS ``plainTex``: strip TeX control words / delimiters."""
    s = re.sub(r"\\[a-zA-Z]+\s?", " ", str(s or ""))
    s = re.sub(r"[{}$\\]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _gv_esc(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def _dot_label(title: str) -> str:
    """Wrap a title to <=3 lines of ~20 chars — mirror of the JS ``gmDotLabel``."""
    words = _plain_tex(title).split()
    lines: list[str] = []
    cur = ""
    MAX = 20
    for w in words:
        c = (cur + " " + w) if cur else w
        if len(c) <= MAX or not cur:
            cur = c
        else:
            lines.append(cur)
            cur = w
            if len(lines) >= 2:
                break
    if cur and len(lines) < 3:
        lines.append(cur)
    if len(lines) >= 3 and len(lines[2]) > MAX:
        lines[2] = lines[2][:MAX - 1] + "…"
    return "\\n".join(_gv_esc(x) for x in lines)


def _gv_green(pct: int) -> str:
    """Progress colour ramp, a near-white green -> _GFILL["done"] — mirror of
    the JS ``gvGreen``."""
    t = max(0.0, min(1.0, pct / 100))
    ramp = ((0xEC, 0x66), (0xF3, 0xBB), (0xEC, 0x6A))
    return "#" + "".join("%02x" % round(a + (b - a) * t) for a, b in ramp)


class _Model:
    """The node/edge/status model, computed exactly as the JS ``gmBuild`` +
    ``gmChapters`` + ``gmStatuses`` do, so the DOT below matches byte-for-byte."""

    def __init__(self, data: dict):
        entries = data.get("entries", [])
        chapters = data.get("chapters") or []
        loc = data.get("loc") or {}
        self.entries = entries
        self.ids = [e["id"] for e in entries]
        self.n = len(entries)
        idx = {e["id"]: i for i, e in enumerate(entries)}

        # edges: (source_i, target_i, type) — "source uses target"; dedup
        self.edges: list[tuple[int, int, str]] = []
        seen: set[tuple[int, int]] = set()
        for e in entries:
            s = idx[e["id"]]
            for d in (e.get("deps") or []):
                t = idx.get(d["id"])
                if t is None or t == s or (s, t) in seen:
                    continue
                seen.add((s, t))
                self.edges.append((s, t, d.get("type") or "uses"))

        # compact chapter index, in first-appearance order
        def key_of(e):
            i = e["id"]
            if loc.get(i) is not None:
                return loc[i]
            return e.get("chapter") if e.get("chapter") is not None else "·"

        self.order: list = []
        seen_ch: dict = {}
        self.ch_of = [0] * self.n
        for i, e in enumerate(entries):
            k = key_of(e)
            if k not in seen_ch:
                seen_ch[k] = len(self.order)
                self.order.append(k)
            self.ch_of[i] = seen_ch[k]
        self._chapters = chapters

        # used-by count: how foundational a node is (`level` is set on the
        # entries by dashboard._assign_levels before this model is built)
        usedby = [0] * self.n
        for s, t, _ in self.edges:
            usedby[t] += 1
        self.usedby = usedby

        # statuses (border = statement, fill = proof)
        deps_adj: list[list[int]] = [[] for _ in range(self.n)]
        for s, t, _ in self.edges:
            deps_adj[s].append(t)
        self.deps_adj = deps_adj
        F = [entries[i].get("lean_status") in _DONE for i in range(self.n)]
        self.F = F
        self.closed = self._closures(deps_adj, F)

        self.stmt: list[str] = []
        self.proof: list[str] = []
        for i in range(self.n):
            ls = entries[i].get("lean_status") or "empty"
            D = deps_adj[i]
            all_lean = all(entries[t].get("lean_status") != "empty" for t in D)
            all_f = all(F[t] for t in D)
            self.stmt.append(
                "formalized" if ls != "empty"
                else ("ready" if (not D or all_lean) else "blocked"))
            if self.closed[i]:
                self.proof.append("done")
            elif F[i]:
                self.proof.append("local")
            elif ls == "sorry":
                self.proof.append("incomplete")
            elif ls == "empty" and (not D or all_f):
                self.proof.append("ready")
            else:
                self.proof.append("notready")

    def _closures(self, deps_adj, F) -> list[bool]:
        """Iterative (cycle-safe) closure: a node is closed iff it is locally done
        and every dependency is closed. Matches the JS ``closed`` DFS, including
        its rule that a back-edge (node currently on the stack) counts as its own
        ``F``. Iterative to avoid recursion limits on deep chains."""
        n = self.n
        memo: list[bool | None] = [None] * n
        onstack = [False] * n
        for root in range(n):
            if memo[root] is not None:
                continue
            stack = [(root, 0)]
            onstack[root] = True
            while stack:
                i, ci = stack[-1]
                if not F[i]:
                    memo[i] = False
                    onstack[i] = False
                    stack.pop()
                    continue
                D = deps_adj[i]
                advanced = False
                while ci < len(D):
                    t = D[ci]
                    ci += 1
                    if memo[t] is None and not onstack[t]:
                        stack[-1] = (i, ci)
                        stack.append((t, 0))
                        onstack[t] = True
                        advanced = True
                        break
                if advanced:
                    continue
                # all deps resolved: closed iff every dep resolves closed
                r = True
                for t in D:
                    dep_closed = memo[t] if memo[t] is not None else F[t]  # back-edge → F[t]
                    if not dep_closed:
                        r = False
                        break
                memo[i] = r
                onstack[i] = False
                stack.pop()
        return [bool(x) for x in memo]

    def ch_label(self, compact: int) -> str:
        """"Chapter 3 · Curvature" — mirror of the JS ``chLabel``. The two must
        agree: the all-collapsed graph is this precomputed SVG, and expanding
        anything swaps in a client-rendered one, so a label that differed would
        visibly change under the reader."""
        k = self.order[compact]
        if isinstance(k, int) and 0 <= k < len(self._chapters):
            c = self._chapters[k]
            title = _plain_tex(c.get("title"))
            num = c.get("num")
            return ("Chapter %s · %s" % (num, title)) if num else title
        return _plain_tex(str(k))[:30]

    def _node_line(self, i: int) -> str:
        e = self.entries[i]
        is_def = (e.get("kind") in _DEF_KINDS)
        fill = _GFILL.get(self.proof[i], _GFILL["notready"])
        border = _GBORDER.get(self.stmt[i], _GBORDER["blocked"])
        shape = "box" if is_def else "ellipse"
        style = '"rounded,filled"' if is_def else '"filled"'
        label = _dot_label(e.get("title") or e.get("label") or e["id"])
        return ('"%s" [shape=%s,style=%s,fillcolor="%s",color="%s",'
                'fontcolor="%s",label="%s"];'
                % (self.ids[i], shape, style, fill, border, _NODE_TEXT, label))


def _dot_clustered(m: _Model, unit_of, n_units: int, label_fn) -> str:
    """Full graph, every node a box, one ``subgraph cluster`` per unit (the
    chapter each node belongs to, per ``unit_of``)."""
    by_u: dict[int, list[int]] = {}
    for i in range(m.n):
        by_u.setdefault(unit_of[i], []).append(i)
    out = ['strict digraph "" {',
           '  rankdir=TB;bgcolor="transparent";pack=true;packmode="clust";splines=true;nodesep=0.3;ranksep=0.5;',
           '  node [shape=box,style="rounded,filled",fontname="Helvetica",fontsize=11,margin="0.11,0.05",penwidth=1.8];',
           '  edge [color="' + _EDGE + '",arrowhead=vee,arrowsize=0.7,penwidth=1];',
           '  graph [fontname="Helvetica",fontsize=13,labeljust="l"];']
    for u in sorted(by_u):
        out.append('  subgraph cluster_%d {' % u)
        out.append('    label="%s";style="rounded,filled";fillcolor="%s";'
                   'color="%s";penwidth=1.4;fontcolor="%s";'
                   % (_gv_esc(label_fn(u)), _GROUP_CLUSTER["fill"],
                      _GROUP_CLUSTER["border"], _GROUP_CLUSTER["text"]))
        for i in by_u[u]:
            out.append('    ' + m._node_line(i))
        out.append('  }')
    for si, ti, ty in m.edges:
        out.append('  "%s" -> "%s"%s;' % (m.ids[ti], m.ids[si], ' [style=dashed]' if ty == "uses" else ''))
    out.append('}')
    return "\n".join(out) + "\n"


def _dot_overview(m: _Model, unit_of, n_units: int, label_fn, id_prefix: str) -> str:
    """One super-node per unit; edges = inter-unit dependency counts. Node ids are
    ``<id_prefix><i>`` so the client can recognise them and drill in."""
    count = [0] * n_units
    done = [0] * n_units
    for i in range(m.n):
        u = unit_of[i]
        count[u] += 1
        if m.closed[i]:
            done[u] += 1
    em: dict[tuple[int, int], int] = {}
    for s, t, _ in m.edges:
        a, b = unit_of[s], unit_of[t]
        if a == b:
            continue
        em[(b, a)] = em.get((b, a), 0) + 1
    out = ['strict digraph "" {',
           '  rankdir=TB;bgcolor="transparent";splines=true;nodesep=0.45;ranksep=0.65;',
           '  node [shape=box,style="rounded,filled",fontname="Helvetica",fontsize=12,margin="0.2,0.13",penwidth=1.8];',
           '  edge [color="' + _EDGE + '",arrowhead=vee,arrowsize=0.85,penwidth=1.2];',
           '  graph [fontname="Helvetica"];']
    for u in range(n_units):
        if not count[u]:
            continue
        # half-up, like JS Math.round — Python's round() is half-to-even, and
        # an exact-.5 percentage must not differ between this precomputed SVG
        # and the client's live re-render (graphDot.ts dotMixed)
        pct = int(100 * done[u] / count[u] + 0.5)
        lbl = _gv_esc(label_fn(u))
        # purple = "this is a chapter" (matches the client's expand-in-place
        # coloring exactly, so the instant precomputed first paint and any
        # later live re-render after expanding look like the same design);
        # a semantic group instead ramps green by how much of it is done.
        is_ch = id_prefix == "ch"
        fill, border = ((_CLUSTER["fill"], _CLUSTER["border"]) if is_ch
                        else (_gv_green(pct), "#5e8777"))
        out.append('  "%s%d" [label="%s\\n%d statements · %d%%",fillcolor="%s",'
                   'color="%s",penwidth=2.6,fontcolor="%s",tooltip="%s — click to expand"];'
                   % (id_prefix, u, lbl, count[u], pct, fill, border,
                      _CLUSTER["text"] if is_ch else _NODE_TEXT, lbl))
    for (b, a), w in em.items():
        extra = (' [penwidth=%.1f]' % min(4.0, 1 + math.sqrt(w))) if w > 1 else ''
        out.append('  "%s%d" -> "%s%d"%s;' % (id_prefix, b, id_prefix, a, extra))
    out.append('}')
    return "\n".join(out) + "\n"


def _dot_full(m: _Model) -> str:
    return _dot_clustered(m, m.ch_of, len(m.order), m.ch_label)


def _dot_chapter_overview(m: _Model) -> str:
    return _dot_overview(m, m.ch_of, len(m.order), m.ch_label, "ch")


def _clean_svg(svg: str) -> str:
    """Drop the XML prolog / DOCTYPE so the SVG embeds directly in HTML."""
    i = svg.find("<svg")
    return svg[i:] if i != -1 else svg


def _run_dot(dot: str) -> str | None:
    try:
        r = subprocess.run(["dot", "-Tsvg"], input=dot, capture_output=True,
                           text=True, timeout=180, check=True)
        return _clean_svg(r.stdout)
    except Exception:
        return None


def render_svgs(data: dict | None) -> dict:
    """Return the precomputed positioned SVG, laid out by ``dot`` at build
    time: ``overview``, the collapsed chapter overview the graph modal opens on
    — an instant first paint with no WASM layout. (A ``full`` per-node SVG
    used to be emitted too, but no frontend code ever read it: it was a
    wasted ``dot`` run and, at hundreds of statements, several hundred KB of
    dead payload in every data.json.) Returns ``{}`` when ``dot`` is
    unavailable — the client then lays the same overview out live with its
    bundled WASM Graphviz."""
    if not data or not data.get("entries") or not dot_available():
        return {}
    try:
        m = _Model(data)
    except Exception:
        return {}
    out = {}
    overview = _run_dot(_dot_chapter_overview(m))
    if overview:
        out["overview"] = overview
    return out
