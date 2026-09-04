"""Deterministic blueprint checks — reproducible, no LLM.

These back the redesign's two reproducible checks (``dag-consistency`` and
``blueprint-lint``) as pure functions over the DAG JSON produced by
the hgraph DAG builder. They are deliberately code, not
descriptor subagents, so their findings never vary between runs. The
descriptor/LLM subagents complement these; they do not replace them.
"""

from __future__ import annotations

from typing import Any

# Blueprint environments that represent a formalisation obligation — a statement
# that should get a ``\lean`` link and eventually a proof. Prose environments
# (remark, example, notation, convention) carry no obligation, so they must not
# count toward coverage/"todo" totals nor be offered a Lean/DAG link.
COUNTABLE_KINDS: frozenset[str] = frozenset(
    {"theorem", "lemma", "proposition", "corollary", "definition"}
)


def is_countable(node: dict[str, Any]) -> bool:
    """True when a node is a formalisation obligation (not prose like a remark)."""
    # Published DAGs historically used both ``type`` and ``kind``.  Prefer a
    # known value from either field so old cache files can be filtered without
    # a resync; an unrecognised value is deliberately not guessed as a theorem.
    for key in ("type", "kind", "content_type"):
        value = str(node.get(key, "")).lower()
        if value in COUNTABLE_KINDS:
            return True
    return False


def find_cycle(dag: dict[str, Any]) -> list[str] | None:
    """Return one dependency cycle as an id path (``a -> b -> a``), or None.

    Iterative DFS (explicit stack) rather than recursive: a deep dependency
    chain must not blow Python's recursion limit and abort the blueprint
    checks on a large workspace.
    """
    adjacency: dict[str, list[str]] = {}
    for edge in dag.get("edges", []):
        adjacency.setdefault(edge["source"], []).append(edge["target"])

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    for start in (n["id"] for n in dag.get("nodes", [])):
        if color.get(start, WHITE) != WHITE:
            continue
        color[start] = GRAY
        path = [start]
        stack = [(start, iter(adjacency.get(start, [])))]
        while stack:
            _, neighbours = stack[-1]
            descended = False
            for nxt in neighbours:
                state = color.get(nxt, WHITE)
                if state == GRAY:  # back-edge into the active path → cycle
                    return path[path.index(nxt):] + [nxt]
                if state == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(adjacency.get(nxt, []))))
                    descended = True
                    break
            if not descended:
                color[path.pop()] = BLACK
                stack.pop()
    return None


def dag_consistency_issues(dag: dict[str, Any]) -> list[str]:
    """Structural problems in the dependency graph: dangling uses and cycles."""
    issues = [
        f"dangling use: {d['node']} uses undefined {d['uses']}"
        for d in dag.get("dangling", [])
    ]
    cycle = find_cycle(dag)
    if cycle:
        issues.append("dependency cycle: " + " -> ".join(cycle))
    return issues


def blueprint_lint_issues(dag: dict[str, Any]) -> list[str]:
    """Statement-level *defects*: soundness problems worth flagging to agents.

    A ``proved`` node that depends on an unproved node is a real
    inconsistency (a formalised result resting on an unformalised one). Missing
    ``\\lean`` links are NOT defects — in an in-progress blueprint most nodes are
    legitimately not yet formalised, so they are reported as coverage (see
    :func:`blueprint_coverage`) rather than flooding the issue count with
    hundreds of "no \\lean link" lines that mislead agents into thinking the
    blueprint is broken."""
    by_id = {n["id"]: n for n in dag.get("nodes", [])}
    # Dependencies live in `edges`, not on the node: an edge is
    # dependency -> dependent, so invert it to get "what this node uses".
    uses: dict[str, list[str]] = {}
    for edge in dag.get("edges", []):
        uses.setdefault(edge["target"], []).append(edge["source"])
    issues: list[str] = []
    for node in dag.get("nodes", []):
        if not node.get("proved"):
            continue
        for used in uses.get(node["id"], ()):
            dep = by_id.get(used)
            if dep is not None and not dep.get("proved"):
                issues.append(f"{node['id']}: proved but depends on unproved {used}")
    return issues


def blueprint_coverage(dag: dict[str, Any]) -> dict[str, int]:
    """Formalisation coverage, reported as progress — never as defects.

    Counts only *countable* nodes (theorems/lemmas/defs, …); prose environments
    like remarks are excluded. ``unlinked`` counts nodes with no ``\\lean`` link
    (work remaining, not a bug); ``proved`` counts formalised nodes; ``sorry``
    counts nodes whose Lean exists but is incomplete; ``total`` is the countable
    node count."""
    nodes = [n for n in dag.get("nodes", []) if is_countable(n)]
    unlinked = sum(1 for n in nodes if not n.get("lean_name"))
    proved = sum(1 for n in nodes if n.get("proved"))
    sorries = sum(1 for n in nodes if n.get("has_sorry"))
    return {
        "total": len(nodes),
        "unlinked": unlinked,
        "proved": proved,
        # Linked to Lean that exists but is incomplete — the "started, not
        # finished" bucket, invisible in `unlinked`/`proved` alone.
        "sorry": sorries,
    }
