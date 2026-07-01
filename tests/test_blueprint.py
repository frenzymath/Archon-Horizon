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
    # definition, lemma, theorem. The proof is folded into the theorem it
    # proves rather than becoming a standalone node.
    assert len(bp.nodes) == 3
    assert [n.kind for n in bp.nodes] == ["definition", "lemma", "theorem"]


def test_source_macros_are_metadata_and_fold_from_proofs() -> None:
    bp = parse_blueprint(
        r"\begin{theorem}\label{t}\source{paper:page-0007}stmt\end{theorem}"
        r"\begin{proof}\source{paper:page-0008, notes:page-0001}pf\end{proof}"
    )
    node = bp.node("t")
    assert node.sources == ("paper:page-0007", "paper:page-0008", "notes:page-0001")
    assert "\\source" not in node.statement
    dag_node = next(n for n in build_dag(bp)["nodes"] if n["id"] == "t")
    assert dag_node["sources"] == list(node.sources)


def test_proof_uses_fold_into_statement() -> None:
    # A proof's \uses become extra dependencies of the statement it proves,
    # deduplicated against the statement's own \uses.
    bp = parse_blueprint(
        r"\begin{theorem}\label{t}\uses{a}stmt\end{theorem}"
        r"\begin{proof}\uses{b, a}\leanok pf\end{proof}"
    )
    assert len(bp.nodes) == 1
    t = bp.node("t")
    assert t.uses == ("a", "b")
    # \leanok inside the proof marks the statement formalised (proof complete).
    assert t.leanok is True


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


def test_mathlibok_parsed_and_in_dag() -> None:
    bp = parse_blueprint(
        r"\begin{lemma}\label{l}\lean{Foo}\mathlibok In mathlib already.\end{lemma}"
    )
    assert bp.node("l").mathlibok is True
    assert bp.node("l").leanok is False
    assert "\\mathlibok" not in bp.node("l").statement
    node = next(n for n in build_dag(bp)["nodes"] if n["id"] == "l")
    assert node["mathlibok"] is True


def test_label_less_node_gets_synthetic_id() -> None:
    bp = parse_blueprint(r"\begin{remark}No label here.\end{remark}")
    assert len(bp.nodes) == 1
    assert bp.nodes[0].id == "node-1"


def test_dag_edges_and_dangling() -> None:
    dag = build_dag(_parsed())

    # The proof folds into thm:main, so no synthetic proof node appears.
    assert {n["id"] for n in dag["nodes"]} == {"def:affine", "lem:glue", "thm:main"}

    edges = {(e["source"], e["target"]) for e in dag["edges"]}
    # used -> dependent direction; lem:glue used by both thm:main and its proof.
    assert ("def:affine", "lem:glue") in edges
    assert ("lem:glue", "thm:main") in edges

    # def:nonexistent is referenced but never defined.
    assert dag["dangling"] == [{"node": "thm:main", "uses": "def:nonexistent"}]
