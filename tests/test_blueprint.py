"""Blueprint parser + DAG: a realistic multi-environment chapter."""

from __future__ import annotations

from archon_horizon.blueprint import Blueprint, build_dag, parse_blueprint

CHAPTER = r"""
\section{Schemes}

% a stray comment line that must be dropped
\begin{definition}[Affine scheme]
  \label{def:affine}
  \lean{AlgebraicGeometry.Scheme.affine}
  \leanok
  An affine scheme is $\operatorname{Spec} R$ for a ring $R$. % trailing comment
\end{definition}

\begin{lemma}
  \label{lem:glue}
  \uses{def:affine, }
  \leanok
  Affine schemes glue. We keep the literal \% sign here.
\end{lemma}

\begin{theorem}[Main]
  \label{thm:main}
  \uses{lem:glue, def:nonexistent}
  \notready
  The category of schemes is nice.
  \begin{proof}
    \uses{lem:glue}
    Omitted.
  \end{proof}
\end{theorem}
"""


def _parsed() -> Blueprint:
    return parse_blueprint(CHAPTER)


def test_node_count_and_kinds() -> None:
    bp = _parsed()
    # definition, lemma, theorem, and the nested proof.
    assert len(bp.nodes) == 4
    assert [n.kind for n in bp.nodes] == ["definition", "lemma", "theorem", "proof"]


def test_metadata_extraction() -> None:
    bp = _parsed()
    affine = bp.node("def:affine")
    assert affine.title == "Affine scheme"
    assert affine.lean == "AlgebraicGeometry.Scheme.affine"
    assert affine.leanok is True
    assert affine.notready is False
    # Metadata commands are stripped from the statement; prose remains.
    assert "\\label" not in affine.statement
    assert "Spec" in affine.statement

    main = bp.node("thm:main")
    assert main.title == "Main"
    assert main.notready is True
    assert main.leanok is False


def test_uses_parsed_dropping_empties() -> None:
    bp = _parsed()
    # Trailing empty token after the comma must be dropped.
    assert bp.node("lem:glue").uses == ("def:affine",)
    assert bp.node("thm:main").uses == ("lem:glue", "def:nonexistent")


def test_label_less_node_gets_synthetic_id() -> None:
    bp = parse_blueprint(r"\begin{remark}No label here.\end{remark}")
    assert len(bp.nodes) == 1
    assert bp.nodes[0].id == "node-1"


def test_dag_edges_and_dangling() -> None:
    dag = build_dag(_parsed())

    # The nested proof carries no \label, so it gets a synthetic id.
    assert {n["id"] for n in dag["nodes"]} == {"def:affine", "lem:glue", "thm:main", "node-1"}

    edges = {(e["source"], e["target"]) for e in dag["edges"]}
    # used -> dependent direction; lem:glue used by both thm:main and its proof.
    assert ("def:affine", "lem:glue") in edges
    assert ("lem:glue", "thm:main") in edges

    # def:nonexistent is referenced but never defined.
    assert dag["dangling"] == [{"node": "thm:main", "uses": "def:nonexistent"}]
