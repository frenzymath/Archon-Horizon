"""hgraph Lean declaration extraction: _root_. escape, structure kinds, Unicode."""

from __future__ import annotations

from archon_horizon.hgraph.sync import _tex_lean_status, parse_lean


def test_tex_lean_status_requires_leanok_for_lean_ok() -> None:
    # I-0410: a node whose \lean target compiles sorry-free but that carries no
    # \leanok must NOT be reported lean_ok — it is `linked` (attached, compiles,
    # not certified). lean_ok requires BOTH \leanok and a sorry-free resolution.
    resolved = {"Demo.foo": "lean_ok"}
    base = {"lean": ["Demo.foo"], "mathlibok": False}
    assert _tex_lean_status({**base, "leanok": True}, resolved)[0] == "lean_ok"
    assert _tex_lean_status({**base, "leanok": False}, resolved)[0] == "linked"
    # A lying \leanok over a sorry stays `sorry`; a missing target stays `empty`.
    assert _tex_lean_status({**base, "leanok": True}, {"Demo.foo": "sorry"})[0] == "sorry"
    assert _tex_lean_status({**base, "leanok": True}, {})[0] == "empty"
    # \mathlibok still wins outright.
    assert _tex_lean_status({**base, "leanok": False, "mathlibok": True}, resolved)[0] == "mathlib_ok"


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


def test_docstring_prose_does_not_become_a_ghost_declaration() -> None:
    # A module/decl docstring whose prose begins with a Lean keyword must not be
    # parsed as a declaration: `class can carry ...` used to invent `class can`
    # (I-0613), and a backticked cross-reference used to invent a decl (I-0472).
    src = (
        "/-- We ask whether the\n"
        "class can carry an effective witness with `h1 = 0`.\n"
        "See `def exists_generic_rank_comparison` for the real statement. -/\n"
        "theorem realThing : True := trivial\n"
    )
    names = _by_name(src)
    assert names == {"realThing": "theorem"}
    assert "can" not in names
    assert "exists_generic_rank_comparison" not in names


def test_block_comment_hides_commented_out_code() -> None:
    # Commented-out declarations (block and line comments) are not real nodes.
    src = (
        "/- theorem oldProof : False := sorry -/\n"
        "-- def scratchpadHelper : Nat := 0\n"
        "def keeper : Nat := 1\n"
    )
    names = _by_name(src)
    assert names == {"keeper": "def"}


def test_plain_block_comment_does_not_empty_following_bodies() -> None:
    # Regression: a plain `/- … -/` (or module `/-! … -/`) above a later decl
    # used to walk upward past it to an earlier `/--`, setting the *previous*
    # decl's body end before its start and writing empty hgraph node bodies.
    src = (
        "/-- Doc for first. -/\n"
        "theorem first : True := by\n"
        "  trivial\n"
        "\n"
        "/-! Module note that is not a decl doc. -/\n"
        "\n"
        "/- Plain comment above second. -/\n"
        "def second : Nat := 0\n"
        "\n"
        "/-- Doc for third. -/\n"
        "theorem third : True := trivial\n"
    )
    by_name = {d["fqname"]: d for d in parse_lean(src)}
    assert set(by_name) == {"first", "second", "third"}
    assert "trivial" in by_name["first"]["body"]
    assert "def second" in by_name["second"]["body"]
    assert by_name["second"]["doc"] == ""  # plain /- is not a docstring
    assert "theorem third" in by_name["third"]["body"]
    assert by_name["third"]["doc"] == "Doc for third."
    assert by_name["first"]["doc"] == "Doc for first."


def test_nested_block_comments_are_balanced() -> None:
    src = (
        "/- outer /- inner class Nope -/ still commented def AlsoNope -/\n"
        "theorem after : True := trivial\n"
    )
    names = _by_name(src)
    assert names == {"after": "theorem"}


def test_declaration_with_trailing_line_comment_still_parses() -> None:
    names = _by_name("def kept : Nat := 0  -- theorem NotReal\n")
    assert names == {"kept": "def"}
    assert "NotReal" not in names


def test_private_declarations_are_identified_for_coverage_filtering() -> None:
    declarations = parse_lean(
        "namespace Demo\n"
        "private theorem helper : True := by trivial\n"
        "theorem publicResult : True := by trivial\n"
        "end Demo\n"
    )
    by_name = {declaration["fqname"]: declaration for declaration in declarations}
    assert by_name["Demo.helper"]["private"] is True
    assert by_name["Demo.publicResult"]["private"] is False
