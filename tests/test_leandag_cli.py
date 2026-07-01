"""leandag command graph-combination helpers."""

from __future__ import annotations

from archon_horizon.commands.leandag import _combine


def _node(node_id: str, source: str, **kw):
    return {
        "id": node_id,
        "type": "lemma",
        "title": node_id,
        "statement": kw.pop("statement", ""),
        "uses": [],
        "lean_source": source,
        **kw,
    }


def test_union_merges_same_id_with_metadata_drift() -> None:
    dags = {
        "a": {"nodes": [_node("Foo.bar", "lemma Foo.bar : Nat := by sorry", title="A")], "edges": []},
        "b": {"nodes": [_node("Foo.bar", "lemma Foo.bar : Nat := by exact 0", title="B")], "edges": []},
    }

    combined = _combine(dags, intersect=False)

    assert [n["id"] for n in combined["nodes"]] == ["Foo.bar"]
    assert combined["nodes"][0]["projects"] == ["a", "b"]
    assert combined["meta"]["signature_conflicts"] == []


def test_union_reports_signature_conflict() -> None:
    dags = {
        "a": {"nodes": [_node("Foo.bar", "lemma Foo.bar : Nat := by sorry")], "edges": []},
        "b": {"nodes": [_node("Foo.bar", "lemma Foo.bar : Int := by sorry")], "edges": []},
    }

    combined = _combine(dags, intersect=False)

    conflicts = combined["meta"]["signature_conflicts"]
    assert conflicts == [{
        "id": "Foo.bar",
        "signatures": {"a": "lemma Foo.bar : Nat", "b": "lemma Foo.bar : Int"},
    }]
