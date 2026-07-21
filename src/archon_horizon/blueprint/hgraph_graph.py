"""Rich dependency graph via hgraph (https://github.com/AxelDlv00/hgraph).

hgraph keeps a plain-files semantic graph under ``<project>/hgraph/`` — one
Markdown node per statement/Lean declaration, typed edges (hard ``uses`` +
soft ``formalizes``), and per-node comments/reviews agents attach with the
``horizon graph`` CLI. Graph sync reconciles the leanblueprint LaTeX and the
Lean sources into it, deriving each node's real ``lean_status`` from the code
(never trusting ``\\leanok``).

This adapter runs a sync and re-serialises the graph into Horizon's published
``{nodes, edges, dangling, meta}`` shape. The implementation is vendored under
``archon_horizon.hgraph``; there is no external package or fallback engine.

Convention notes (they differ from Horizon's):
- hgraph edge direction is dependent → dependency (``a --uses--> b`` means *a
  needs b*); Horizon's DAG edges run dependency → dependent, so edges are
  inverted here.
- hgraph node ids are opaque 12-char hashes; Horizon nodes are keyed by the
  blueprint ``\\label``, kept in each tex node's ``meta.label``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_ENTRY_NAMES = ("web.tex", "print.tex", "content.tex")
_PROVED = ("lean_ok", "mathlib_ok")


def _detect_entry(project_root: Path, blueprint_dir: Path | None) -> Path | None:
    """The blueprint's entry ``.tex`` — the file whose ``\\input`` tree is the
    document. hgraph flattens from one entry, unlike a plain glob-and-concatenate.

    Falls back to a lone ``.tex`` in the blueprint directory: a small or young
    blueprint is often a single file under no particular name, and it has an
    unambiguous entry even though it matches none of ``_ENTRY_NAMES``. With
    several ``.tex`` files and no recognised entry there is no way to know the
    document order, so that stays unresolved rather than guessed.
    """
    dirs: list[Path] = []
    if blueprint_dir is not None:
        dirs += [blueprint_dir, blueprint_dir / "src"]
    dirs += [project_root / "blueprint" / "src", project_root / "blueprint"]
    for directory in dirs:
        for name in _ENTRY_NAMES:
            candidate = directory / name
            if candidate.exists():
                return candidate
    for directory in dirs:
        if not directory.is_dir():
            continue
        tex = sorted(directory.glob("*.tex"))
        if len(tex) == 1:
            return tex[0]
    return None


def _project_macros(project_root: Path, blueprint_dir: Path | None) -> dict[str, str]:
    """KaTeX macros for statement rendering, from the blueprint's .tex files."""
    from archon_horizon.blueprint.chapters import parse_macros

    macros: dict[str, str] = {}
    dirs = [d for d in (blueprint_dir, project_root / "blueprint" / "src", project_root / "blueprint") if d]
    for directory in dirs:
        if not directory.is_dir():
            continue
        for tex in sorted(directory.glob("*.tex")):
            try:
                macros.update(parse_macros(tex.read_text("utf-8", errors="replace")))
            except OSError:
                continue
        if macros:
            break
    return macros


def _attachment_count(graph, node_id: str, prefix: str) -> int:
    """Number of ``<prefix>-N.md`` attachments (comments / reviews) on a node."""
    try:
        node_dir = graph.nodes_dir / node_id
        return sum(1 for p in node_dir.glob(f"{prefix}-*.md")) if node_dir.is_dir() else 0
    except OSError:
        return 0


def build_project_graph(
    project_root: Path, blueprint_dir: Path | None = None, *, sync: bool = True
) -> dict[str, Any] | None:
    """Build the hgraph-backed graph for one project, or None when it has none.

    ``sync=True`` reconciles the blueprint + Lean sources into
    ``<project>/hgraph/`` first (idempotent; authored fields like comments and
    reviews are never touched). Returns ``{"nodes","edges","dangling","meta"}``
    with edges as dependency→dependent ``source/target``.

    None means exactly one thing: **this project has no blueprint entry**, so
    there is no graph to build. Every other failure raises. A sync error is a
    real fault — swallowing it would silently serve an empty/stale DAG and look
    like the graph itself regressed.
    """
    from archon_horizon.hgraph import Graph
    from archon_horizon.hgraph.analysis import Analysis
    from archon_horizon.hgraph.sync import load_config as hgraph_config
    from archon_horizon.hgraph.sync import sync as hgraph_sync

    project_root = Path(project_root)
    cfg = hgraph_config(project_root)
    entry = Path(cfg["blueprint"]) if cfg.get("blueprint") else _detect_entry(project_root, blueprint_dir)
    if entry is None or not entry.exists():
        return None
    lean_paths = cfg.get("lean") or [str(project_root)]

    warnings: list[str] = []
    graph = Graph.open(project_root)
    if sync:
        outcome = hgraph_sync(graph, blueprint=str(entry), lean_paths=lean_paths, root=project_root)
        warnings = list(outcome.get("warnings") or ())
    analysis = Analysis(graph)
    tex_nodes = list(graph.nodes(type="tex"))

    # Only labelled statements are blueprint nodes; unlabelled tex nodes
    # (source quotes, standalone proof bodies) are hgraph-internal.
    tex_nodes = [n for n in tex_nodes if n.meta.get("label")]
    label_of = {n.id: str(n.meta.get("label")) for n in tex_nodes}
    lean_by_id = {n.id: n for n in graph.nodes(type="lean")}

    # One edges() pass for everything below. hgraph re-reads and YAML-parses
    # every edge file on each edges() call, so per-node graph.descendants()
    # (which calls edges() internally) is O(V·E) file reads — 200+ seconds on a
    # ~750-statement blueprint. Counting descendants from this single snapshot
    # keeps the whole build linear in the size of the graph.
    all_edges = list(graph.edges())

    # Lean linkage: formalizes edges join a tex statement to its Lean forms.
    formalized: dict[str, list[Any]] = {}
    uses_pairs: list[tuple[str, str]] = []
    for edge in all_edges:
        if edge.type == "formalizes":
            tex_id, lean_id = (
                (edge.source, edge.target) if edge.target in lean_by_id else (edge.target, edge.source)
            )
            if tex_id in label_of and lean_id in lean_by_id:
                formalized.setdefault(tex_id, []).append(lean_by_id[lean_id])
        elif edge.type == "uses" and edge.source in label_of and edge.target in label_of:
            # hgraph: source needs target. Horizon: dependency → dependent.
            uses_pairs.append((label_of[edge.target], label_of[edge.source]))

    # descendant_count for every node from the snapshot above — mirrors
    # graph.descendants() (reverse reachability over hard edges) without its
    # per-call full re-read of the edge files.
    rev_hard: dict[str, list[str]] = {}
    for edge in all_edges:
        if edge.hard:
            rev_hard.setdefault(edge.target, []).append(edge.source)

    def _descendant_count(start: str) -> int:
        seen = {start}
        stack = [start]
        count = 0
        while stack:
            for nxt in rev_hard.get(stack.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
                    count += 1
        return count

    nodes: list[dict[str, Any]] = []
    for node in sorted(tex_nodes, key=lambda n: (str(n.meta.get("chapter") or ""), n.meta.get("order") or 0)):
        meta = node.meta
        status = str(meta.get("lean_status") or "empty")
        leans = formalized.get(node.id, [])
        lean_names = [str(l.meta.get("decl")) for l in leans if l.meta.get("decl")]
        mathlib_name = meta.get("mathlib_name")
        nodes.append({
            "id": label_of[node.id],
            "type": str(meta.get("content_type") or "statement"),
            "title": node.title or "",
            "chapter": str(meta.get("chapter") or ""),
            "group": meta.get("group"),
            "level": meta.get("level"),
            "order": meta.get("order"),
            "statement": node.content or "",
            "uses": [],  # edges carry the dependency structure
            "sources": [meta["ref"]] if meta.get("ref") else [],
            "lean_name": ", ".join(lean_names) or (
                ", ".join(mathlib_name) if isinstance(mathlib_name, list) else mathlib_name
            ),
            "proved": status in _PROVED,
            "mathlib_ok": status == "mathlib_ok" or bool(mathlib_name),
            "lean_status": status,
            "state": analysis.states.get(node.id),
            "unlocks": analysis.unlocks(node.id),
            "lean_source": "\n\n".join(l.content for l in leans if l.content) or None,
            "has_sorry": status == "sorry" or any(
                l.meta.get("lean_status") == "sorry" for l in leans
            ),
            "lean_file": next((l.meta.get("file") for l in leans if l.meta.get("file")), None),
            "dep_count": len(analysis.deps.get(node.id, ())),
            "rdep_count": len(analysis.rdeps.get(node.id, ())),
            "descendant_count": _descendant_count(node.id),
            "comment_count": _attachment_count(graph, node.id, "comment"),
            "review_count": _attachment_count(graph, node.id, "review"),
            "hgraph_id": node.id,  # for `hgraph … <id>` without a label lookup
        })

    seen: set[tuple[str, str]] = set()
    edges = []
    for source, target in uses_pairs:
        if (source, target) in seen or source == target:
            continue
        seen.add((source, target))
        edges.append({"source": source, "target": target})

    # Port hgraph's granularity heuristic into the published node shape. Authored
    # \group/\level values win; missing levels would otherwise all default to
    # "fine" in the chapter UI, making its coarse/medium controls empty.
    from archon_horizon.hgraph.dashboard import _assign_groups_levels

    deps_by_node: dict[str, list[dict[str, str]]] = {node["id"]: [] for node in nodes}
    for edge in edges:
        deps_by_node[edge["target"]].append({"id": edge["source"]})
    graph_entries = [
        {
            "id": node["id"],
            "chapter": node["chapter"],
            "kind": node["type"],
            "group": node["group"],
            "level": node["level"],
            "deps": deps_by_node[node["id"]],
        }
        for node in nodes
    ]
    _assign_groups_levels(graph_entries)
    enriched = {entry["id"]: entry for entry in graph_entries}
    for node in nodes:
        node["group"] = enriched[node["id"]]["group"]
        node["level"] = enriched[node["id"]]["level"]

    # Unresolved \uses/\lean references surface as sync warnings; keep them in
    # the shape the parser DAG uses ({"node", "uses"}) where parseable.
    dangling = []
    for warning in warnings:
        head, _, rest = warning.partition(":")
        if "\\uses{" in rest:
            used = rest.split("\\uses{", 1)[1].split("}", 1)[0]
            dangling.append({"node": head.strip(), "uses": used})

    return {
        "nodes": nodes,
        "edges": edges,
        "dangling": dangling,
        "meta": {
            "engine": "hgraph",
            "entry": str(entry),
            "macros": _project_macros(project_root, blueprint_dir),
            "states": analysis.state_counts(),
            "warnings": warnings[:50],
        },
    }
