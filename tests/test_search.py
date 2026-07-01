"""Offline Lean declaration search: extraction, BM25/name/type queries, caching."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import load_config
from archon_horizon.search.index import LeanSearchIndex, extract_declarations
from archon_horizon.search.workspace import load_or_build_index, resolve_source_roots

SNIPPET = r"""
import Mathlib

namespace Topology

/-- A continuous function on a compact set has compact image. -/
theorem IsCompact.image {f : α → β} (hf : Continuous f) (hs : IsCompact s) :
    IsCompact (f '' s) := by
  sorry

@[simp]
lemma foo_bar : 1 + 1 = 2 := rfl

def myDef (n : Nat) : Nat := n + 1

end Topology

structure Point where
  x : Nat
  y : Nat
"""


def _index() -> LeanSearchIndex:
    decls = extract_declarations(SNIPPET, library="demo", file="Demo.lean")
    return LeanSearchIndex(decls)


def test_signature_keeps_named_argument_assignments() -> None:
    # A `:=` inside an argument (a named/default value like `(I := …)`) must NOT
    # truncate the signature — only the top-level `:=` (the body) terminates it.
    text = (
        "/-- doc -/\n"
        "def foo (g : M) (h : Witness (I := bar) (p := q)) {t : ℝ} : Nat := 0\n"
    )
    (decl,) = extract_declarations(text, library="demo", file="D.lean")
    assert "Witness (I := bar) (p := q)" in decl.signature
    assert ": Nat" in decl.signature
    assert decl.signature.endswith(": Nat")  # the body `:= 0` is cut, nothing after


def test_signature_spanning_lines_with_inner_assignment() -> None:
    text = (
        "/-- d -/\n"
        "theorem bar (a : A)\n"
        "    (h : Foo (x := 1))\n"
        "    : Prop := by trivial\n"
    )
    (decl,) = extract_declarations(text, library="demo", file="D.lean")
    assert "Foo (x := 1)" in decl.signature and ": Prop" in decl.signature


def test_extract_declarations_names_kinds_doc_namespace() -> None:
    decls = {d.name: d for d in extract_declarations(SNIPPET, library="demo", file="Demo.lean")}
    assert set(decls) == {"Topology.IsCompact.image", "Topology.foo_bar", "Topology.myDef", "Point"}
    assert decls["Topology.IsCompact.image"].kind == "theorem"
    assert "compact image" in decls["Topology.IsCompact.image"].doc.lower()
    assert "Continuous f" in decls["Topology.IsCompact.image"].signature
    assert decls["Topology.foo_bar"].kind == "lemma"  # attribute line didn't break it
    assert decls["Point"].kind == "structure"


def test_search_text_ranks_relevant_first() -> None:
    hits = _index().search_text("compact image of a continuous function", limit=3)
    assert hits
    assert hits[0].declaration.name == "Topology.IsCompact.image"


def test_search_name_matches_segment() -> None:
    hits = _index().search_name("foo_bar")
    assert hits[0].declaration.name == "Topology.foo_bar"


def test_search_type_pattern_wildcards() -> None:
    hits = _index().search_type("Continuous ?f")
    names = {h.declaration.name for h in hits}
    assert "Topology.IsCompact.image" in names


def test_jsonl_round_trip() -> None:
    index = _index()
    restored = LeanSearchIndex.from_jsonl(index.to_jsonl())
    assert [d.name for d in restored.declarations] == [d.name for d in index.declarations]


WORKSPACE_CONFIG = """
workspace:
  name: search-ws
  ground_agent:
    harness: g
  horizon_agent:
    harness: h
harnesses:
  g:
    kind: "null"
  h:
    kind: "null"
external_libraries:
  - name: mathlib
    rev: v4.20.0
projects:
  proj:
    path: projects/proj
"""


def _make_workspace(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(WORKSPACE_CONFIG, "utf-8")
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    (proj / "Main.lean").write_text("def projThing : Nat := 0\n", "utf-8")
    # A fetched mathlib under .lake/packages, plus build artifacts that must be ignored.
    pkg = proj / ".lake" / "packages" / "mathlib" / "Mathlib"
    pkg.mkdir(parents=True)
    (pkg / "Compact.lean").write_text(
        "/-- compactness -/\ntheorem isCompact_singleton : True := trivial\n", "utf-8"
    )
    build = proj / ".lake" / "build" / "lib"
    build.mkdir(parents=True)
    (build / "Generated.lean").write_text("def shouldBeIgnored : Nat := 1\n", "utf-8")


def test_resolve_source_roots_finds_project_and_package(tmp_path: Path) -> None:
    _make_workspace(tmp_path)
    cfg = load_config(tmp_path)
    roots = resolve_source_roots(tmp_path, cfg)
    assert "proj" in roots and "mathlib" in roots
    assert roots["mathlib"].name == "mathlib"


def test_index_build_excludes_build_artifacts_and_caches(tmp_path: Path) -> None:
    _make_workspace(tmp_path)
    cfg = load_config(tmp_path)
    index = load_or_build_index(tmp_path, cfg)

    names = {d.name for d in index.declarations}
    assert "projThing" in names
    assert "isCompact_singleton" in names
    assert "shouldBeIgnored" not in names  # under .lake/build, skipped

    # The cache was written and is reused without rebuilding.
    cache = tmp_path / ".archon-horizon" / "search" / "index.jsonl"
    assert cache.exists()
    again = load_or_build_index(tmp_path, cfg)
    assert {d.name for d in again.declarations} == names
