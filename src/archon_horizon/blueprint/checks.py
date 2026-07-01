"""Deterministic blueprint checks — reproducible, no LLM.

These back the redesign's two reproducible checks (``dag-consistency`` and
``blueprint-lint``) as pure functions over the DAG JSON produced by
:func:`archon_horizon.blueprint.dag.build_dag`. They are deliberately code, not
descriptor subagents, so their findings never vary between runs. The
descriptor/LLM subagents complement these; they do not replace them.
"""

from __future__ import annotations

from typing import Any


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
    """Statement-level lint: missing ``\\lean`` links and leanok inconsistencies."""
    by_id = {n["id"]: n for n in dag.get("nodes", [])}
    issues: list[str] = []
    for node in dag.get("nodes", []):
        if not node.get("lean"):
            issues.append(f"{node['id']}: no \\lean link")
        if node.get("leanok"):
            for used in node.get("uses", []):
                dep = by_id.get(used)
                if dep is not None and not dep.get("leanok"):
                    issues.append(f"{node['id']}: leanok but depends on not-leanok {used}")
    return issues
