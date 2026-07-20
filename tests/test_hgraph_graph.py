"""The hgraph adapter: sync a project and serialize the Horizon DAG shape.

The graph engine is vendored into Horizon, so these run unconditionally: if the
internal package cannot be imported that is a real failure, not a reason to skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest


from archon_horizon.blueprint.hgraph_graph import build_project_graph

_BLUEPRINT = r"""
\chapter{Basics}
\begin{definition}[The basic foo]\label{def:foo}
  \dcref{ch1:1.1}\group{Basics}\level{coarse}
  \lean{Demo.foo}\leanok
  A foo is a natural number.
\end{definition}
\begin{theorem}\label{thm:bar}
  \uses{def:foo}
  \lean{Demo.bar}
  Every foo is a bar.
\end{theorem}
\begin{proof}
  \uses{def:missing}
  Obvious.
\end{proof}
"""

_LEAN = """
namespace Demo
def foo : Nat := 1
theorem bar : foo = 1 := by sorry
end Demo
"""


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    bp = tmp_path / "blueprint" / "src"
    bp.mkdir(parents=True)
    (bp / "content.tex").write_text(_BLUEPRINT, "utf-8")
    (tmp_path / "Demo.lean").write_text(_LEAN, "utf-8")
    return tmp_path


def test_adapter_emits_horizon_dag_shape(project: Path) -> None:
    dag = build_project_graph(project)
    assert dag is not None
    assert dag["meta"]["engine"] == "hgraph"

    by_id = {n["id"]: n for n in dag["nodes"]}
    assert set(by_id) == {"def:foo", "thm:bar"}

    foo = by_id["def:foo"]
    assert foo["proved"] is True            # def foo has no sorry -> lean_ok
    assert foo["lean_name"] == "Demo.foo"
    assert "Nat := 1" in (foo["lean_source"] or "")
    assert foo["group"] == "Basics"
    assert foo["level"] == "coarse"
    assert foo["sources"] == ["ch1:1.1"]

    bar = by_id["thm:bar"]
    assert bar["proved"] is False           # its Lean proof is a sorry
    assert bar["has_sorry"] is True
    assert bar["state"] in ("ready", "blocked", "formalized_open")
    assert bar["level"] == "medium"  # inferred by hgraph's granularity heuristic

    # Horizon direction: dependency -> dependent.
    assert {"source": "def:foo", "target": "thm:bar"} in dag["edges"]
    # The unresolved proof \uses{def:missing} surfaces as dangling.
    assert any(d["uses"] == "def:missing" for d in dag["dangling"])

    # Idempotent: a second sync+build gives the same node set.
    again = build_project_graph(project)
    assert {n["id"] for n in again["nodes"]} == set(by_id)


def test_adapter_descendant_counts_match_graph(project: Path) -> None:
    """The adapter counts descendants from one in-memory edge snapshot (per-node
    graph.descendants() re-reads every edge file — O(V·E), minutes on a large
    blueprint). The counts must stay identical to hgraph's own traversal."""
    from archon_horizon.hgraph import Graph

    dag = build_project_graph(project)
    graph = Graph.open(project)
    for node in dag["nodes"]:
        assert node["descendant_count"] == len(graph.descendants(node["hgraph_id"]))
    by_id = {n["id"]: n for n in dag["nodes"]}
    assert by_id["def:foo"]["descendant_count"] == 1  # thm:bar transitively needs it
    assert by_id["thm:bar"]["descendant_count"] == 0


def test_adapter_counts_node_attachments(project: Path) -> None:
    from archon_horizon.hgraph import Graph

    build_project_graph(project)  # create + sync the graph
    graph = Graph.open(project)
    node_id = graph.resolve("label:thm:bar")
    node_dir = graph.nodes_dir / node_id
    node_dir.mkdir(exist_ok=True)
    (node_dir / "comment-1.md").write_text("---\nauthor: agent\n---\ntried simp, failed\n", "utf-8")

    dag = build_project_graph(project, sync=False)
    bar = next(n for n in dag["nodes"] if n["id"] == "thm:bar")
    assert bar["comment_count"] == 1
    assert bar["hgraph_id"] == node_id
