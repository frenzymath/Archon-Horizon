"""Blueprint wired to the workspace + the deterministic subagents."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.blueprint.workspace import workspace_dags
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.subagents.base import SubagentContext
from archon_horizon.subagents.builtins import BlueprintLintSubagent, DagConsistencySubagent

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


def test_dag_consistency_finds_cycle(tmp_path: Path) -> None:
    result = DagConsistencySubagent().run(SubagentContext(_workspace(tmp_path)))
    assert not result.ok
    assert any("cycle" in i.body.lower() for i in result.issues)


def test_blueprint_lint_flags_missing_lean_and_leanok_dep(tmp_path: Path) -> None:
    result = BlueprintLintSubagent().run(SubagentContext(_workspace(tmp_path)))
    bodies = " ".join(i.body for i in result.issues)
    assert "no \\lean link" in bodies          # c (theorem) has no \lean
    assert "leanok" in bodies                   # a is leanok but depends on not-leanok c
