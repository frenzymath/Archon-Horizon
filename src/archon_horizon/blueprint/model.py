"""Blueprint data model.

A blueprint is a set of amsthm-style nodes (theorem/lemma/definition/…) plus
the leanblueprint metadata that ties them to Lean and to each other. We keep
the *parsed* form deliberately small and structural: enough to build the DAG
and, later, to render HTML from the same parse. Frozen + slotted to match the
``core`` style — parsed blueprints are immutable values that flow through the
pipeline unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class BlueprintNode:
    """One amsthm environment.

    ``id`` comes from ``\\label`` (synthesized when absent so every node is
    addressable). ``uses`` are the labels this node depends on (from
    ``\\uses``); ``lean`` is the linked declaration (``\\lean``). ``leanok`` /
    ``notready`` carry formalisation status. ``statement`` is the env body with
    the metadata commands stripped out.
    """

    id: str
    kind: str
    statement: str
    title: str | None = None
    lean: str | None = None
    uses: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    leanok: bool = False
    notready: bool = False
    # ``\\mathlibok``: the statement already exists in mathlib (rendered blue).
    mathlibok: bool = False


@dataclass(frozen=True, slots=True)
class Blueprint:
    nodes: tuple[BlueprintNode, ...] = field(default_factory=tuple)

    @property
    def by_id(self) -> dict[str, BlueprintNode]:
        return {node.id: node for node in self.nodes}

    def node(self, node_id: str) -> BlueprintNode:
        return self.by_id[node_id]
