"""The dashboard search blends Lean declarations with blueprint LaTeX."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.server.service import WorkspaceService

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""

_LEAN = """\
/-- The foo lemma about a trivial equality. -/
theorem Foo : 1 = 1 := rfl
"""

_TEX = r"""
\begin{theorem}\label{thm:foo}\lean{Foo}\leanok
A statement about the commutativity of addition.
\end{theorem}
"""


def _make(tmp_path: Path) -> WorkspaceService:
    ws = tmp_path / "ws"
    proj = ws / "projects" / "ag-main"
    (proj / "blueprint").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    (proj / "Foo.lean").write_text(_LEAN, "utf-8")
    (proj / "blueprint" / "ch1.tex").write_text(_TEX, "utf-8")
    return WorkspaceService(ws)


def test_name_search_finds_lean_declaration(tmp_path: Path) -> None:
    service = _make(tmp_path)
    res = service.search("Foo", mode="name")
    names = [r["name"] for r in res["results"]]
    assert "Foo" in names
    hit = next(r for r in res["results"] if r["name"] == "Foo")
    # The Lean hit carries its linked blueprint statement (LaTeX) for the UI.
    assert hit["blueprint"] is not None
    assert "commutativity" in hit["blueprint"]["statement"]


def test_informal_text_search_matches_blueprint_latex(tmp_path: Path) -> None:
    service = _make(tmp_path)
    # "commutativity" appears only in the blueprint LaTeX, not the Lean source —
    # informal search must still surface the declaration via the blueprint.
    res = service.search("commutativity of addition", mode="text")
    assert res["results"], "expected a blueprint-driven match"
    top = res["results"][0]
    assert top["blueprint"] and "commutativity" in top["blueprint"]["statement"]


def test_empty_query_returns_no_results(tmp_path: Path) -> None:
    service = _make(tmp_path)
    assert service.search("  ", mode="text")["results"] == []


_LEAN_COVERAGE = """\
/-- A fact about normalized factors only. -/
theorem normalizedFact : True := trivial

/-- A fact about a normalized geodesic curve. -/
theorem geodesicNormalized : True := trivial
"""


def test_text_search_ranks_by_query_term_coverage(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    proj = ws / "projects" / "ag-main"
    proj.mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    (proj / "Geo.lean").write_text(_LEAN_COVERAGE, "utf-8")
    service = WorkspaceService(ws)

    res = service.search("normalized geodesic", mode="text")
    names = [r["name"] for r in res["results"]]
    # The declaration matching BOTH words must beat the one matching only one,
    # even though "normalized" appears in both.
    assert names[0] == "geodesicNormalized"
    assert names.index("geodesicNormalized") < names.index("normalizedFact")


def test_search_filters_by_kind(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    proj = ws / "projects" / "ag-main"
    proj.mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    (proj / "K.lean").write_text(
        "/-- a normalized theorem -/\ntheorem normThm : True := trivial\n"
        "/-- a normalized def -/\ndef normDef : Nat := 0\n",
        "utf-8",
    )
    service = WorkspaceService(ws)
    res = service.search("normalized", mode="text", kinds=["def"])
    kinds = {r["kind"] for r in res["results"]}
    names = {r["name"] for r in res["results"]}
    assert kinds == {"def"}
    assert "normDef" in names and "normThm" not in names

    # Selecting several kinds shows ALL of them (an OR filter), not just one.
    both = service.search("normalized", mode="text", kinds=["def", "theorem"])
    both_names = {r["name"] for r in both["results"]}
    assert {"normDef", "normThm"} <= both_names


def test_state_exposes_search_libraries(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    libs = WorkspaceService(ws).state()["libraries"]
    assert "ag-main" in libs


def test_search_endpoint_routes_through_serve_endpoint(tmp_path: Path) -> None:
    service = _make(tmp_path)
    payload = service.serve_endpoint("/api/search?q=Foo&mode=name&limit=5")
    assert payload["mode"] == "name"
    assert any(r["name"] == "Foo" for r in payload["results"])
    # The search endpoint is live-only and must NOT be in the static export set.
    assert not any(ep.startswith("/api/search") for ep in service.endpoints())
