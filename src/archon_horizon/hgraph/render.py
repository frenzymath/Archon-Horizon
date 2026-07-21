"""Human-facing rendering: `get` output and the tex / lean / union views."""

from __future__ import annotations

import textwrap
from collections import Counter

from .graph import ATTACHMENT_KINDS, Edge, Graph, Node

_PROVENANCE = ("author", "created", "updated")


# --------------------------------------------------------------------------- #
# structured (JSON) serialisation — for agents / scripts (`--json`)
# --------------------------------------------------------------------------- #
def node_json(n: Node) -> dict:
    return {"id": n.id, "type": n.type, "title": n.title,
            "content": n.content, **n.meta}


def edge_json(e: Edge) -> dict:
    return {"id": e.id, "source": e.source, "target": e.target,
            "type": e.type, "hard": e.hard, **e.attrs}


def get_json(g: Graph, nid: str) -> dict:
    """The full neighbourhood of a node as one JSON-able dict — the structured
    twin of :func:`render_get` (node, soft links, hard deps, ancestors, notes)."""
    n = g.get_node(nid)
    d = node_json(n)
    d["soft_out"] = [edge_json(e) for e in g.successors(nid, hard=False)]
    d["soft_in"] = [edge_json(e) for e in g.predecessors(nid, hard=False)]
    d["depends_on"] = [e.target for e in g.successors(nid, hard=True)]
    d["depended_on_by"] = [e.source for e in g.predecessors(nid, hard=True)]
    d["ancestors"] = g.ancestors(nid)
    for kind in ATTACHMENT_KINDS:
        d[kind + "s"] = [node_json(a) for a in g.attachments(nid, kind)]
    return d


def stats_json(g: Graph) -> dict:
    """Aggregate counts an agent needs to steer: totals, and breakdowns by node
    type, formalization status, review verdict, plus stale / edge tallies."""
    nodes = list(g.nodes())
    edges = g.edges()
    by_type = Counter(n.type or "?" for n in nodes)
    by_lean = Counter(n.meta.get("lean_status", "—") for n in nodes if n.type == "tex")
    by_status = Counter(n.meta.get("status") for n in nodes if n.meta.get("status"))
    reviewed = sum(1 for n in nodes if g.attachments(n.id, "review"))
    return {
        "nodes": len(nodes), "edges": len(edges),
        "hard_edges": sum(1 for e in edges if e.hard),
        "stale": sum(1 for n in nodes if n.meta.get("stale")),
        "reviewed": reviewed,
        "by_type": dict(sorted(by_type.items())),
        "tex_lean_status": dict(sorted(by_lean.items())),
        "by_status": dict(sorted(by_status.items())),
    }


def _snippet(text: str, n: int = 90) -> str:
    line = next((l for l in text.splitlines() if l.strip()), "")
    return (line[:n] + "…") if len(line) > n else line


def _cluster_label(g: Graph, comp: set[str]) -> str:
    nodes = [g.get_node(x) for x in sorted(comp)]
    rep = next((x for x in nodes if x.type == "tex"), nodes[0])
    types = "+".join(sorted({x.type for x in nodes if x.type}))
    return f"{rep.title or rep.id} [{types}]"


def render_get(g: Graph, nid: str) -> str:
    n = g.get_node(nid)
    out = [f"● {n.title or nid}   [{n.type or '?'}]   id: {nid}"]
    shown = {k: v for k, v in n.meta.items() if k not in _PROVENANCE}
    if shown:
        out.append("  meta: " + ", ".join(f"{k}={v}" for k, v in shown.items()))
    prov = [f"{k} {n.meta[k]}" for k in _PROVENANCE if n.meta.get(k)]
    if prov:
        out.append("  " + " · ".join(prov))
    if n.content.strip():
        out.append("\n" + textwrap.indent(n.content.strip(), "  "))

    soft_out = g.successors(nid, hard=False)
    soft_in = g.predecessors(nid, hard=False)
    if soft_out or soft_in:
        out.append("\nrelated (soft links):")
        for e in soft_out:
            t = g.get_node(e.target)
            out.append(f"  {e.type} → {e.target} [{t.type}]: {_snippet(t.content)}")
        for e in soft_in:
            s = g.get_node(e.source)
            out.append(f"  {e.type} ← {e.source} [{s.type}]: {_snippet(s.content)}")

    deps = g.successors(nid, hard=True)
    dependents = g.predecessors(nid, hard=True)
    if deps:
        out.append("\ndepends on: " + ", ".join(e.target for e in deps))
    if dependents:
        out.append("depended on by: " + ", ".join(e.source for e in dependents))
    anc = g.ancestors(nid)
    if anc:
        out.append(f"all ancestors ({len(anc)}): " + ", ".join(anc))

    comments = g.attachments(nid, "comment")
    if comments:
        out.append("\ncomments:")
        for c in comments:
            who = c.meta.get("author") or "anon"
            when = c.meta.get("date", "")
            title = c.meta.get("title")
            head = f"  - [{when} · {who}]" + (f" {title}:" if title else "")
            out.append(f"{head} {c.content.strip()}")

    reviews = g.attachments(nid, "review")
    if reviews:
        out.append("\nreviews:")
        for r in reviews:
            who = r.meta.get("author") or "anon"
            when = r.meta.get("date", "")
            axes = []
            if r.meta.get("maths_verdict"):
                axes.append(f"maths: {r.meta['maths_verdict']}"
                           + (f" — {r.meta['maths_comment']}" if r.meta.get("maths_comment") else ""))
            if r.meta.get("lean_verdict"):
                axes.append(f"lean: {r.meta['lean_verdict']}"
                           + (f" — {r.meta['lean_comment']}" if r.meta.get("lean_comment") else ""))
            out.append(f"  - [{when} · {who}]  " + "  ".join(axes))
    return "\n".join(out)


def view_text(g: Graph, kind: str) -> str:
    if kind == "union":
        comps, sedges = g.union_view()
        out = [f"union view — {len(comps)} conceptual node(s) "
               "(soft-linked representations merged):"]
        for i, comp in enumerate(comps):
            out.append(f"  [{i}] {_cluster_label(g, comp)}  "
                       f"{{{', '.join(sorted(comp))}}}")
        out.append("\ndependencies between conceptual nodes:")
        out += [f"  [{i}] --{t}--> [{j}]" for i, j, t in sedges] or ["  (none)"]
        return "\n".join(out)

    out = [f"{kind} view (primary nodes of type '{kind}'):"]
    for n in g.nodes(type=kind):
        out.append(f"● {n.title or n.id} ({n.id})")
        for e in g.successors(n.id, hard=False) + g.predecessors(n.id, hard=False):
            other = e.target if e.source == n.id else e.source
            arrow = "→" if e.source == n.id else "←"
            out.append(f"    ~ {e.type} {arrow} {other} [{g.get_node(other).type}]")
        for e in g.successors(n.id, hard=True):
            out.append(f"    ⇒ {e.type} {e.target}")
    return "\n".join(out)


def view_dot(g: Graph, kind: str) -> str:
    esc = lambda s: s.replace('"', '\\"')
    if kind == "union":
        comps, sedges = g.union_view()
        out = ["digraph hgraph {", "  rankdir=LR;",
               "  node [shape=box, style=rounded];"]
        for i, comp in enumerate(comps):
            out.append(f'  c{i} [label="{esc(_cluster_label(g, comp))}"];')
        out += [f'  c{i} -> c{j} [label="{esc(t)}"];' for i, j, t in sedges]
        out.append("}")
        return "\n".join(out)

    out = ["digraph hgraph {", "  rankdir=LR;",
           "  node [shape=box, style=rounded];"]
    for n in g.nodes():
        hl = ' style="rounded,filled" fillcolor="#e6f0ff"' if n.type == kind else ""
        out.append(f'  "{n.id}" [label="{esc(n.title or n.id)}\\n[{n.type}]"{hl}];')
    for e in g.edges():
        style = "solid" if e.hard else "dashed"
        out.append(f'  "{e.source}" -> "{e.target}" '
                   f'[label="{esc(e.type)}", style={style}];')
    out.append("}")
    return "\n".join(out)
