"""Blueprint wired to the workspace (hgraph-only) + the deterministic checks.

The DAG is hgraph's and nothing else, so these exercise hgraph's node shape:
`type` (not `kind`), `proved` (not `leanok`), `lean_name` (not `lean`), and
dependencies carried in `edges` rather than on the node. hgraph resolves
`\\lean{...}` against the real Lean sources, so a node is only linked/proved when
the declaration actually exists — that is the point of dropping the parser.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.blueprint.checks import blueprint_coverage, blueprint_lint_issues, dag_consistency_issues
from archon_horizon.blueprint.workspace import project_dag, published_dag, workspace_dags
from archon_horizon.core.workspace import Project, Workspace

# `a` claims \leanok and is backed by real Lean; `c` is not formalised at all.
# a -> uses c, so `a` is proved while resting on an unproved node: a real defect.
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

LEAN = """\
theorem A : 1 = 1 := rfl
theorem B : 2 = 2 := sorry
"""


def _workspace(tmp_path: Path) -> Workspace:
    proj = tmp_path / "projects" / "ag-main"
    (proj / "blueprint").mkdir(parents=True)
    (proj / "blueprint" / "content.tex").write_text(CYCLE_TEX, "utf-8")
    (proj / "Main.lean").write_text(LEAN, "utf-8")
    return Workspace(
        name="ws",
        root=tmp_path,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"),
                                     blueprint_path=Path("projects/ag-main/blueprint"))},
    )


def test_workspace_dags_built_from_sources(tmp_path: Path) -> None:
    dags = workspace_dags(_workspace(tmp_path))
    assert "ag-main" in dags
    # Nodes are keyed by their LaTeX label, not hgraph's internal hash.
    assert {n["id"] for n in dags["ag-main"]["nodes"]} == {"a", "b", "c"}
    assert (dags["ag-main"].get("meta") or {}).get("engine") == "hgraph"


def test_published_cache_filters_legacy_prose_nodes(tmp_path: Path) -> None:
    """A dashboard cache from before the prose boundary must be harmless."""
    ws = _workspace(tmp_path)
    cache = ws.state_path / "blueprints"
    cache.mkdir(parents=True)
    (cache / "ag-main.json").write_text(
        '{"nodes": ['
        '{"id": "rem:x", "type": "remark"},'
        '{"id": "thm:x", "type": "theorem"}], '
        '"edges": [{"source": "rem:x", "target": "thm:x"}]}'
    )
    dag = published_dag(ws, "ag-main")
    assert [node["id"] for node in dag["nodes"]] == ["thm:x"]
    assert dag["edges"] == []


def test_workspace_dags_scoped_to_projects(tmp_path: Path) -> None:
    # A run scoped to one project must not rebuild the others.
    ws = _workspace(tmp_path)
    other = tmp_path / "projects" / "other" / "blueprint"
    other.mkdir(parents=True)
    (other / "content.tex").write_text(r"\begin{lemma}\label{x}X.\end{lemma}", "utf-8")
    ws.projects["other"] = Project(
        name="other", path=Path("projects/other"), blueprint_path=Path("projects/other/blueprint")
    )

    assert set(workspace_dags(ws, ["ag-main"])) == {"ag-main"}
    assert set(workspace_dags(ws)) == {"ag-main", "other"}
    assert workspace_dags(ws, ["nonexistent"]) == {}


def test_undeclared_project_gets_no_dag(tmp_path: Path) -> None:
    # config.yaml is the source of truth: a blueprint in an undeclared directory
    # is not discovered. (The parser used to scan the workspace root for these.)
    ws = _workspace(tmp_path)
    stray = tmp_path / "stray" / "blueprint"
    stray.mkdir(parents=True)
    (stray / "content.tex").write_text(r"\begin{lemma}\label{y}Y.\end{lemma}", "utf-8")
    assert set(workspace_dags(ws)) == {"ag-main"}


def test_lean_link_resolves_against_real_sources(tmp_path: Path) -> None:
    dag = project_dag(_workspace(tmp_path), "ag-main")
    assert dag is not None
    by_id = {n["id"]: n for n in dag["nodes"]}
    # `A` exists and is proved; `B` exists but is a sorry; `c` has no \lean at all.
    assert by_id["a"]["lean_name"] == "A"
    assert by_id["a"]["proved"] is True
    assert by_id["b"]["has_sorry"] is True
    assert by_id["b"]["proved"] is False
    assert not by_id["c"]["lean_name"]


def test_rich_dag_preserves_source_anchors(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    bp = tmp_path / "projects" / "ag-main" / "blueprint" / "content.tex"
    bp.write_text(r"\begin{lemma}\label{s}\source{ega:page-0042}S.\end{lemma}", "utf-8")
    dag = project_dag(ws, "ag-main")
    assert dag is not None
    node = next(n for n in dag["nodes"] if n["id"] == "s")
    assert node["sources"] == ["ega:page-0042"]


def test_dag_consistency_finds_cycle(tmp_path: Path) -> None:
    dag = project_dag(_workspace(tmp_path), "ag-main")
    issues = dag_consistency_issues(dag)
    assert any("cycle" in i.lower() for i in issues)


def test_blueprint_lint_flags_proved_resting_on_unproved(tmp_path: Path) -> None:
    dag = project_dag(_workspace(tmp_path), "ag-main")
    bodies = " ".join(blueprint_lint_issues(dag))
    # `a` is proved but uses `c`, which has no Lean at all — a soundness defect.
    assert "a: proved but depends on unproved c" in bodies
    # "no \lean link" is coverage (work remaining), NOT a defect: it must not
    # appear in the lint issues reported to agents as problems.
    assert "no \\lean link" not in bodies


def test_blueprint_coverage_counts_unlinked_and_sorries(tmp_path: Path) -> None:
    dag = project_dag(_workspace(tmp_path), "ag-main")
    cov = blueprint_coverage(dag)
    assert cov["total"] >= 1
    assert cov["unlinked"] >= 1   # c (theorem) has no \lean link
    assert cov["sorry"] >= 1      # b is linked to Lean that is still a sorry
    assert 0 <= cov["proved"] <= cov["total"]
