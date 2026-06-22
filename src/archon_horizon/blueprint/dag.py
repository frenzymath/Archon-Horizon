"""Build a JSON-able dependency graph from a parsed blueprint.

Generated directly from the same parse that feeds rendering, so the DAG never
depends on ``leanblueprint web`` / plasTeX / graphviz. Edge convention: a
``\\uses{a}`` on node ``n`` means ``n`` depends on ``a``, so the edge points
from the dependency to the dependent (``a -> n``) — a topological order, and
the direction leanblueprint's depgraph uses.
"""

from __future__ import annotations

from typing import Any

from .model import Blueprint


def build_dag(bp: Blueprint) -> dict[str, Any]:
    ids = set(bp.by_id)

    nodes = [
        {
            "id": node.id,
            "kind": node.kind,
            "title": node.title,
            "lean": node.lean,
            "leanok": node.leanok,
            "notready": node.notready,
        }
        for node in bp.nodes
    ]

    edges: list[dict[str, str]] = []
    dangling: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in bp.nodes:
        for used in node.uses:
            if used not in ids:
                dangling.append({"node": node.id, "uses": used})
                continue
            edge = (used, node.id)
            if edge in seen:
                continue
            seen.add(edge)
            edges.append({"source": used, "target": node.id})

    return {"nodes": nodes, "edges": edges, "dangling": dangling}
