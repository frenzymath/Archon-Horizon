"""hgraph Lean declaration extraction: _root_. escape, structure kinds, Unicode."""

from __future__ import annotations

from archon_horizon.hgraph.sync import parse_lean


def _by_name(src: str) -> dict[str, str]:
    return {d["fqname"]: d["kind"] for d in parse_lean(src)}


def test_root_escape_ignores_namespace() -> None:
    src = "namespace Foo\ntheorem _root_.Absolute.qux : True := trivial\nend Foo\n"
    names = _by_name(src)
    assert "Absolute.qux" in names          # absolute, not Foo._root_.Absolute.qux
    assert "Foo._root_.Absolute.qux" not in names


def test_structure_inductive_class_are_extracted() -> None:
    src = (
        "structure Baz where\n  x : Nat\n"
        "inductive Color\n  | red\n  | green\n"
        "class MyCls (a : Type) where\n  op : a\n"
    )
    names = _by_name(src)
    assert names.get("Baz") == "structure"
    assert names.get("Color") == "inductive"
    assert names.get("MyCls") == "class"


def test_unicode_and_subscript_names() -> None:
    src = "namespace N\ndef veca‖x‖ : Nat := 0\ntheorem foo₁ : True := trivial\nend N\n"
    names = _by_name(src)
    assert "N.veca‖x‖" in names
    assert "N.foo₁" in names


def test_anonymous_instance_has_no_decl() -> None:
    # `instance : C` has no name; it must not produce a bogus decl.
    names = _by_name("instance : Inhabited Nat := ⟨0⟩\n")
    assert names == {}
