"""Offline Lean declaration search: extraction, BM25/name/type queries, caching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_horizon.search.index import Declaration, LeanSearchIndex, extract_declarations
from archon_horizon.search.workspace import load_or_build_index, resolve_source_roots

SNIPPET = r"""
import Mathlib

/-! Compactness of continuous images on topological spaces. -/

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
    assert "continuous images" in decls["Topology.IsCompact.image"].header.lower()


def test_search_text_ranks_relevant_first() -> None:
    hits = _index().search_text("compact image of a continuous function", limit=3)
    assert hits
    assert hits[0].declaration.name == "Topology.IsCompact.image"


def test_search_name_matches_segment() -> None:
    hits = _index().search_name("foo_bar")
    assert hits[0].declaration.name == "Topology.foo_bar"
    assert hits[0].score == 100.0


def test_search_name_ranks_prefix_over_substring() -> None:
    hits = _index().search_name("foo")
    assert hits
    assert hits[0].declaration.name == "Topology.foo_bar"
    assert hits[0].score >= 70.0


def test_search_name_does_not_rank_subsequence_near_misses() -> None:
    index = LeanSearchIndex([
        Declaration(
            name="elim_finite_subcover_image", kind="theorem",
            signature="theorem elim_finite_subcover_image : True",
            doc="", library="demo", file="A.lean", line=1,
        ),
        Declaration(
            name="isCompact_image", kind="theorem",
            signature="theorem isCompact_image : True",
            doc="", library="demo", file="B.lean", line=1,
        ),
        Declaration(
            name="Filter.Tendsto.isCompact_image", kind="theorem",
            signature="theorem Filter.Tendsto.isCompact_image : True",
            doc="", library="demo", file="C.lean", line=1,
        ),
    ])
    hits = index.search_name("isCompact_image")
    names = [h.declaration.name for h in hits]
    assert names[0] == "isCompact_image"
    assert "Filter.Tendsto.isCompact_image" in names
    assert "elim_finite_subcover_image" not in names


def test_search_type_pattern_wildcards() -> None:
    hits = _index().search_type("Continuous ?f")
    names = {h.declaration.name for h in hits}
    assert "Topology.IsCompact.image" in names


def test_search_type_empty_or_wildcard_only_is_empty() -> None:
    index = _index()
    assert index.search_type("") == []
    assert index.search_type("?a") == []
    assert index.search_type("_") == []
    index.search_type("?a -> ?b")  # literals present; must not raise


def test_search_type_bounds_wildcards_on_long_signatures() -> None:
    arrows = " -> ".join(f"T{i}" for i in range(80))
    index = LeanSearchIndex([
        Declaration(
            name="long_chain", kind="def",
            signature=f"def long_chain : {arrows}",
            doc="", library="demo", file="Long.lean", line=1,
        ),
        Declaration(
            name="list_map", kind="def",
            signature="def list_map : (α -> β) -> List α -> List β",
            doc="", library="demo", file="Map.lean", line=1,
        ),
    ])
    hits = index.search_type("(?a -> ?b) -> List ?a -> List ?b")
    assert [h.declaration.name for h in hits] == ["list_map"]


def test_mcp_header_mode_returns_module_comment_hits(tmp_path, monkeypatch):
    from archon_horizon.search.mcp_server import _handle

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    (tmp_path / "Demo.lean").write_text(SNIPPET, encoding="utf-8")
    result = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "lean_search", "arguments": {"query": "continuous images", "mode": "header"}}}, tmp_path)
    assert "isError" not in result["result"]
    assert "IsCompact.image" in result["result"]["content"][0]["text"]


def test_inline_attributes_preserve_declarations_and_their_docstrings():
    declarations = extract_declarations(
        "/-- The public result. -/\n@[simp] theorem inline_result : True := trivial\n",
        library="demo", file="Main.lean")
    assert len(declarations) == 1
    assert declarations[0].name == "inline_result" and declarations[0].doc == "The public result."
    assert declarations[0].line == 2


def test_mcp_stdio_recovers_after_invalid_json_and_nonobject_messages(monkeypatch):
    import io
    from archon_horizon.search import mcp_server
    incoming = io.StringIO('[]\n{\n{"jsonrpc":"2.0","id":3,"method":"ping"}\n'
                           '{"jsonrpc":"2.0","method":"unknown-notification"}\n')
    outgoing = io.StringIO()
    monkeypatch.setattr(mcp_server.sys, "stdin", incoming)
    monkeypatch.setattr(mcp_server.sys, "stdout", outgoing)
    assert mcp_server.main() == 0
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert [response.get("error", {}).get("code") for response in responses[:2]] == [-32600, -32700]
    assert responses[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert len(responses) == 3


@pytest.mark.parametrize("limit", [0, -1, 101, True, "10"])
def test_mcp_rejects_invalid_limits_before_building_an_index(tmp_path, monkeypatch, limit):
    from archon_horizon.search.mcp_server import _handle
    def unexpected_build(*args, **kwargs):
        pytest.fail("Invalid tool options must not trigger an index build")
    monkeypatch.setattr("archon_horizon.search.workspace.load_or_build_index", unexpected_build)
    response = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "lean_search", "arguments": {"query": "True", "limit": limit}}}, tmp_path)
    assert response["result"]["isError"] is True
    assert "limit must be an integer" in response["result"]["content"][0]["text"]


def test_search_header_matches_module_comment() -> None:
    hits = _index().search_header("continuous images")
    assert hits
    assert hits[0].declaration.name == "Topology.IsCompact.image"
    assert _index().query("continuous images", mode="header")[0].declaration.name == "Topology.IsCompact.image"


def test_jsonl_round_trip() -> None:
    index = _index()
    restored = LeanSearchIndex.from_jsonl(index.to_jsonl())
    assert [d.name for d in restored.declarations] == [d.name for d in index.declarations]


def test_persisted_bm25_cache_reuses_text_hits(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "Main.lean").write_text(SNIPPET, encoding="utf-8")
    cache = tmp_path / "cache"
    first = load_or_build_index(root, cache_dir=cache)
    assert first.search_text("compact image")[0].declaration.name == "Topology.IsCompact.image"
    dest = next(path for path in cache.iterdir() if path.is_dir() and (path / "text" / "params.index.json").is_file())
    restored = LeanSearchIndex.load_cache(dest, json.loads((dest / "meta.json").read_text())["fingerprint"])
    assert restored is not None
    assert restored.search_text("compact image")[0].declaration.name == "Topology.IsCompact.image"
    assert restored.search_header("continuous images")[0].declaration.name == "Topology.IsCompact.image"


def test_empty_text_query_is_empty() -> None:
    assert _index().search_text("") == []
    assert _index().search_header("   ") == []
    assert _index().search_name("") == []
    assert _index().search_type("   ") == []


def test_empty_workspace_indexes_without_bm25_error(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    index = load_or_build_index(root, cache_dir=tmp_path / "cache")
    assert index.declarations == []
    assert index.search_text("compact") == []


def _make_workspace(tmp_path: Path) -> None:
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    (proj / "lean-toolchain").write_text("leanprover/lean4:v4.20.0\n", "utf-8")
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
    roots = resolve_source_roots(tmp_path)
    assert tmp_path.name in roots and "mathlib" in roots
    assert roots["mathlib"].name == "mathlib"


def test_index_build_extracts_files_in_walk_order(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "B.lean").write_text("def bee : Nat := 0\n", encoding="utf-8")
    nested = root / "sub"
    nested.mkdir()
    (nested / "A.lean").write_text("def aye : Nat := 1\n", encoding="utf-8")
    (root / "C.lean").write_text("def cee : Nat := 2\n", encoding="utf-8")
    index = LeanSearchIndex.build({"project": root}, workspace_root=root)
    assert [d.name for d in index.declarations] == ["bee", "cee", "aye"]


def test_index_build_excludes_build_artifacts_and_caches(tmp_path: Path) -> None:
    _make_workspace(tmp_path)
    cache_dir = tmp_path / "cache"
    index = load_or_build_index(tmp_path, cache_dir=cache_dir)

    names = {d.name for d in index.declarations}
    assert "projThing" in names
    assert "isCompact_singleton" in names
    assert "shouldBeIgnored" not in names  # under .lake/build, skipped

    # The cache was written and is reused without rebuilding.
    assert any(path.is_dir() and (path / "meta.json").is_file() for path in cache_dir.iterdir())
    again = load_or_build_index(tmp_path, cache_dir=cache_dir)
    assert {d.name for d in again.declarations} == names


def test_cache_invalidation_tracks_changed_sources_and_skips_private_state(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "Main.lean"
    source.write_text("def oldName : Nat := 0\n")
    private = root / ".horizon"
    private.mkdir()
    (private / "Scratch.lean").write_text("def privateName : Nat := 1\n")
    cache = tmp_path / "cache"
    assert [d.name for d in load_or_build_index(root, cache_dir=cache).declarations] == ["oldName"]
    source.write_text("def newName : Nat := 0\n")
    assert [d.name for d in load_or_build_index(root, cache_dir=cache).declarations] == ["newName"]


def test_explicit_source_roots_include_external_checkouts(tmp_path):
    root, library = tmp_path / "project", tmp_path / "reference"
    root.mkdir()
    library.mkdir()
    (library / "Ref.lean").write_text("theorem reference : True := trivial\n")
    index = load_or_build_index(root, {"reference": library}, cache_dir=tmp_path / "cache")
    assert index.declarations[0].library == "reference"


def test_cached_declaration_paths_follow_the_requested_workspace_root(tmp_path):
    library = tmp_path / "reference"
    library.mkdir()
    (library / "Ref.lean").write_text("theorem reference : True := trivial\n")
    sources = {"reference": library}
    cache = tmp_path / "cache"
    broad = load_or_build_index(tmp_path, sources, cache_dir=cache)
    narrow = load_or_build_index(library, sources, cache_dir=cache)
    assert broad.declarations[0].file == "reference/Ref.lean"
    assert narrow.declarations[0].file == "Ref.lean"


def test_default_search_cache_stays_outside_the_checkout(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Main.lean").write_text("theorem compact_image : True := trivial\n")
    cache_home = tmp_path / "cache-home"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    first = load_or_build_index(root)
    second = load_or_build_index(root)
    assert [item.name for item in first.declarations] == ["compact_image"]
    assert [item.name for item in second.declarations] == ["compact_image"]
    assert list(root.iterdir()) == [root / "Main.lean"]
    assert list((cache_home / "archon-horizon" / "lean-search").glob("*/meta.json"))


def test_mcp_queries_use_the_worker_checkout_without_manifest(tmp_path, monkeypatch):
    from archon_horizon.search.mcp_server import _handle
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    (tmp_path / "Main.lean").write_text("theorem compact_image : True := trivial\n")
    result = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "lean_search", "arguments": {"query": "compact_image", "mode": "name"}}}, tmp_path)
    assert "isError" not in result["result"]
    assert "compact_image" in result["result"]["content"][0]["text"]


def _lake_project(path: Path, declaration: str, dependencies: list[dict] | None = None) -> Path:
    path.mkdir(parents=True)
    (path / "Main.lean").write_text(f"theorem {declaration} : True := trivial\n")
    (path / "lake-manifest.json").write_text(json.dumps({"packages": dependencies or []}))
    return path


def test_nested_lake_projects_keep_library_ownership_without_duplicate_declarations(tmp_path):
    root = tmp_path / "session-123"
    _lake_project(root / "formalized-sources" / "MorganTian", "morgan_result")
    _lake_project(root / "formalized-sources" / "DoCarmo", "foundation_result")
    (root / "Scratch.lean").write_text("def scratch : Nat := 0\n")

    index = load_or_build_index(root, cache_dir=tmp_path / "cache")
    assert index.library_counts == {"DoCarmo": 1, "MorganTian": 1, "session-123": 1}
    assert index.search_name("foundation_result", library="DoCarmo")[0].declaration.file == (
        "formalized-sources/DoCarmo/Main.lean"
    )
    assert len(index.declarations) == 3


def test_lake_path_dependencies_are_transitive_cycle_safe_and_deduplicated(tmp_path):
    morgan = _lake_project(tmp_path / "MorganTian", "morgan_result", [
        {"type": "path", "name": "DoCarmoLib", "dir": "../DoCarmo"},
        {"type": "path", "name": "missing", "dir": "../missing"},
    ])
    docarmo = _lake_project(tmp_path / "DoCarmo", "foundation_result", [
        {"type": "path", "name": "Shared", "dir": "../shared"},
    ])
    _lake_project(tmp_path / "shared", "shared_result", [
        {"type": "path", "name": "MorganTianLib", "dir": "../MorganTian"},
    ])
    packages = morgan / ".lake" / "packages"
    packages.mkdir(parents=True)
    (packages / "DoCarmoLib").symlink_to(docarmo, target_is_directory=True)

    index = load_or_build_index(morgan, cache_dir=tmp_path / "cache")
    assert index.library_counts == {"DoCarmo": 1, "MorganTian": 1, "shared": 1}
    assert len(index.declarations) == 3


def test_lake_path_dependency_uses_project_directory_name_without_fetched_packages(tmp_path):
    morgan = _lake_project(tmp_path / "MorganTian", "morgan_result", [
        {"type": "path", "name": "DoCarmoLib", "dir": "../DoCarmo"},
    ])
    _lake_project(tmp_path / "DoCarmo", "foundation_result")
    index = load_or_build_index(morgan, cache_dir=tmp_path / "cache")
    hit = index.search_name("foundation_result", library="DoCarmo")[0].declaration
    assert hit.file == str(tmp_path / "DoCarmo" / "Main.lean")


def test_discovery_tolerates_invalid_manifests_and_qualifies_name_collisions(tmp_path):
    for directory in ("a", "b"):
        project = _lake_project(tmp_path / directory / "Library", f"result_{directory}")
        (project / "lake-manifest.json").write_text("not JSON")
    index = load_or_build_index(tmp_path, cache_dir=tmp_path / "cache")
    assert index.library_counts == {"Library": 1, "b/Library:Library": 1}


@pytest.mark.parametrize("mode", ["search_text", "search_name", "search_type", "search_header"])
def test_unknown_library_is_an_error_with_indexed_inventory(mode):
    with pytest.raises(ValueError, match="Unknown Lean library 'missing'. Indexed libraries: demo"):
        getattr(_index(), mode)("compact", library="missing")


def test_mcp_exposes_library_inventory_and_unknown_library_errors(tmp_path, monkeypatch):
    from archon_horizon.search.mcp_server import _handle

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache-home"))
    _lake_project(tmp_path / "DoCarmo", "foundation_result")
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "lean_search", "arguments": {"mode": "libraries"}}}
    result = _handle(request, tmp_path)["result"]
    assert json.loads(result["content"][0]["text"]) == {"DoCarmo": 1}
    request["params"]["arguments"] = {"query": "foundation", "lib": "missing"}
    result = _handle(request, tmp_path)["result"]
    assert result["isError"]
    assert "Indexed libraries: DoCarmo" in result["content"][0]["text"]
