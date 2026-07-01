"""Blueprint wired to the workspace + the deterministic checks."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.blueprint.checks import blueprint_lint_issues, dag_consistency_issues
from archon_horizon.blueprint.workspace import project_dag, project_rich_dag, workspace_dags
from archon_horizon.core.workspace import Project, Workspace

CYCLE_TEX = r"""
\begin{definition}\label{a}\uses{c}\lean{A}\leanok
A.
\end{definition}
\begin{lemma}\label{b}\uses{a}\lean{B}
B.
\end{lemma}
\begin{theorem}\label{c}\uses{b}
C with no lean link.
\end{theorem}
"""


def _workspace(tmp_path: Path) -> Workspace:
    bp = tmp_path / "projects" / "ag-main" / "blueprint"
    bp.mkdir(parents=True)
    (bp / "ch1.tex").write_text(CYCLE_TEX, "utf-8")
    return Workspace(
        name="ws",
        root=tmp_path,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"),
                                     blueprint_path=Path("projects/ag-main/blueprint"))},
    )


def test_workspace_dags_built_from_sources(tmp_path: Path) -> None:
    dags = workspace_dags(_workspace(tmp_path))
    assert "ag-main" in dags
    assert {n["id"] for n in dags["ag-main"]["nodes"]} == {"a", "b", "c"}


def test_rich_dag_preserves_source_anchors(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    bp = tmp_path / "projects" / "ag-main" / "blueprint" / "source.tex"
    bp.write_text(
        r"\begin{lemma}\label{s}\source{ega:page-0042}S.\end{lemma}",
        "utf-8",
    )
    dag = project_rich_dag(ws, "ag-main")
    assert dag is not None
    node = next(n for n in dag["nodes"] if n["id"] == "s")
    assert node["sources"] == ["ega:page-0042"]


def test_dag_consistency_finds_cycle(tmp_path: Path) -> None:
    dag = project_dag(_workspace(tmp_path), "ag-main")
    issues = dag_consistency_issues(dag)
    assert any("cycle" in i.lower() for i in issues)


def test_blueprint_lint_flags_missing_lean_and_leanok_dep(tmp_path: Path) -> None:
    dag = project_dag(_workspace(tmp_path), "ag-main")
    bodies = " ".join(blueprint_lint_issues(dag))
    assert "no \\lean link" in bodies          # c (theorem) has no \lean
    assert "leanok" in bodies                   # a is leanok but depends on not-leanok c
