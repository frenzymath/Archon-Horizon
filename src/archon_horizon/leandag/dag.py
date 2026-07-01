from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .models import BlueprintDecl, Edge, GraphNode, LeanDecl


# Node types whose formalisation work is the *proof*. Everything else
# (definition, notation, conjecture, lean_aux) is measured by the content
# of the declaration itself — it has no proof to write. Remarks never
# become nodes at all (see _build_nodes).
_PROOF_TYPES: frozenset[str] = frozenset({
    "lemma", "theorem", "proposition", "corollary", "exercise",
})


class DAG:
    """
    Dependency graph of mathematical declarations.

    Nodes are :class:`GraphNode` instances.  Edges run from a predecessor to
    every node that depends on it (``\\uses{B}`` inside A creates edge B → A).

    Construction from sources::

        dag = DAG.from_sources(blueprint_decls, proofs, lean_decls)

    Reload from a saved file::

        dag = DAG.load(Path(".leandag/dag.json"))
    """

    def __init__(self, nodes: list[GraphNode], edges: list[Edge]) -> None:
        self._nodes  = nodes
        self._edges  = edges
        self._by_id: dict[str, GraphNode] = {n.id: n for n in nodes}
        # (node_id, lean_ref) for every \lean{} name that matched no Lean
        # declaration — populated by :meth:`from_sources`, empty after load().
        self.unmatched_lean: list[tuple[str, str]] = []
        # LaTeX macro map ({"\\name": "expansion"}) for rendering blueprint
        # notation; populated from the parser at build time, persisted in JSON.
        self.macros: dict[str, str] = {}

    # ── Factories ──────────────────────────────────────────────────────────

    @classmethod
    def from_sources(
        cls,
        blueprint_decls: list[BlueprintDecl],
        proofs: dict[str, str],
        lean_decls: dict[str, LeanDecl],
        macros: Optional[dict[str, str]] = None,
    ) -> DAG:
        """
        Build a DAG from parsed blueprint declarations and Lean declarations.

        Lean declarations not referenced by any blueprint node are added as
        ``lean_aux`` nodes so formalization machinery counts toward complexity.
        """
        nodes = cls._build_nodes(blueprint_decls, proofs, lean_decls)
        cls._compute_degrees(nodes)
        cls._compute_impact(nodes)
        cls._compute_metrics(nodes)
        edges = cls._build_edges(nodes)
        dag = cls(nodes, edges)
        dag.macros = macros or {}
        dag.unmatched_lean = [
            (decl.id, name)
            for decl in blueprint_decls
            for name in decl.lean_names
            if name not in lean_decls
        ]
        return dag

    @classmethod
    def load(cls, path: Path) -> DAG:
        """Reconstruct a :class:`DAG` from a serialised ``dag.json`` file."""
        data  = json.loads(path.read_text(encoding='utf-8'))
        nodes = [GraphNode.from_dict(d) for d in data["nodes"]]
        edges = [Edge(source=e["from"], target=e["to"]) for e in data["edges"]]
        dag = cls(nodes, edges)
        meta = data.get("meta", {})
        dag.macros = meta.get("macros", {})
        dag.unmatched_lean = [tuple(x) for x in meta.get("unmatched_lean", [])]
        return dag

    # ── Public interface ───────────────────────────────────────────────────

    @property
    def nodes(self) -> list[GraphNode]:
        return list(self._nodes)

    @property
    def edges(self) -> list[Edge]:
        return list(self._edges)

    @property
    def axioms(self) -> list[GraphNode]:
        """Nodes with no incoming edges (no blueprint dependencies)."""
        return [n for n in self._nodes if n.dep_count == 0]

    @property
    def leaves(self) -> list[GraphNode]:
        """Nodes that no other node depends on."""
        return [n for n in self._nodes if n.rdep_count == 0]

    @property
    def isolated(self) -> list[GraphNode]:
        """Nodes with no edges at all — no ``\\uses{}`` in or out.

        These declarations were never wired into the graph: nothing depends on
        them and they depend on nothing. A large isolated set usually means the
        dependencies simply haven't been recorded yet.
        """
        return [n for n in self._nodes
                if n.dep_count == 0 and n.rdep_count == 0]

    def node(self, node_id: str) -> GraphNode:
        return self._by_id[node_id]

    def ancestors(self, node_id: str) -> set[str]:
        """Return ``{node_id}`` ∪ all transitive predecessors."""
        uses_of = {n.id: n.uses for n in self._nodes}
        visited: set[str] = {node_id}
        queue   = list(uses_of.get(node_id, []))
        while queue:
            curr = queue.pop()
            if curr not in visited:
                visited.add(curr)
                queue.extend(uses_of.get(curr, []))
        return visited

    def effort_summary(self) -> dict:
        """Project-wide formalisation accounting.

        - ``effort_done``            Σ Lean code size already written
                                     (uncommented, sorry-free declarations).
        - ``effort_remaining_lower`` Σ local effort over nodes with a *finite*
                                     estimate — a lower bound, since nodes with
                                     no estimate (∞) are omitted.
        - ``effort_remaining_unknown_nodes`` how many nodes have ∞ local effort
                                     (no Lean proof and no draft to estimate from).
        - ``effort_remaining``       the lower bound if nothing is ∞, else
                                     ``None`` meaning the true total is unbounded.
        """
        done = sum(n.proof_size_lean for n in self._nodes
                   if n.proof_size_lean is not None)
        finite  = [n.effort_local for n in self._nodes if n.effort_local is not None]
        unknown = sum(1 for n in self._nodes if n.effort_local is None)
        lower   = sum(finite)
        return {
            "effort_done":                  done,
            "effort_remaining_lower":       lower,
            "effort_remaining_unknown_nodes": unknown,
            "effort_remaining":             lower if unknown == 0 else None,
        }

    # ── Assembly (private) ─────────────────────────────────────────────────

    @staticmethod
    def _build_nodes(
        blueprint_decls: list[BlueprintDecl],
        proofs: dict[str, str],
        lean_decls: dict[str, LeanDecl],
    ) -> list[GraphNode]:
        nodes: list[GraphNode] = []
        referenced_lean: set[str] = set()

        # Remarks are prose annotations, not mathematical obligations: they
        # carry no proof, pin no Lean declaration, and would only pollute the
        # graph (isolated nodes, fake gaps). They do not become DAG nodes, and
        # a ``\uses{}`` pointing at a remark label is silently dropped rather
        # than reported as a broken reference. (The parser still consumes the
        # environments so their labels are known here.)
        remark_ids = {d.id for d in blueprint_decls if d.type == "remark"}
        blueprint_decls = [d for d in blueprint_decls if d.type != "remark"]

        for decl in blueprint_decls:
            # A node may point at several Lean declarations (\lean{a, b}); the
            # Lean-side metrics aggregate over every name that resolves.
            matched = [lean_decls[name] for name in decl.lean_names if name in lean_decls]
            referenced_lean.update(name for name in decl.lean_names if name in lean_decls)

            sizes      = [m.proof_size for m in matched]
            proof_size = None if any(s is None for s in sizes) else sum(sizes)

            proof_tex      = proofs.get(decl.id, '')
            proof_size_tex = DAG._count_tex_chars(proof_tex)

            nodes.append(GraphNode(
                id              = decl.id,
                type            = decl.type,
                title           = decl.title,
                chapter         = decl.chapter,
                statement       = decl.statement,
                uses            = [u for u in decl.uses if u not in remark_ids],
                sources         = decl.sources,
                lean_name       = ", ".join(decl.lean_names) or None,
                proved          = decl.is_proved,
                mathlib_ok      = decl.mathlib_ok,
                proof_tex       = proof_tex,
                lean_source     = "\n\n".join(m.source for m in matched),
                proof_size_lean = proof_size if matched else None,
                has_sorry       = any(m.has_sorry for m in matched),
                proof_size_tex  = proof_size_tex,
                tex_file        = decl.tex_file,
                lean_file       = next((m.file for m in matched if m.file), ""),
            ))

        # Lean declarations not referenced in the blueprint: formalization
        # machinery that still contributes to overall complexity.
        for name, lean in sorted(lean_decls.items()):
            if name in referenced_lean:
                continue
            nodes.append(GraphNode(
                id              = f"lean:{name}",
                type            = "lean_aux",
                title           = name,
                chapter         = "",
                statement       = "",
                uses            = [],
                sources         = [],
                lean_name       = name,
                proved          = not lean.has_sorry,
                lean_source     = lean.source,
                proof_size_lean = lean.proof_size,
                has_sorry       = lean.has_sorry,
                lean_file       = lean.file,
            ))

        return nodes

    @staticmethod
    def _compute_degrees(nodes: list[GraphNode]) -> None:
        ids:  set[str]      = {n.id for n in nodes}
        rdep: dict[str, int] = defaultdict(int)
        for n in nodes:
            for pred_id in n.uses:
                if pred_id in ids:
                    rdep[pred_id] += 1
        for n in nodes:
            n.dep_count  = len(n.uses)
            n.rdep_count = rdep[n.id]

    @staticmethod
    def _compute_impact(nodes: list[GraphNode]) -> None:
        """Set ``descendant_count`` — how many nodes transitively depend on each.

        A high value marks a node on the critical path: formalising it unblocks
        many others, so it is a natural focus point.
        """
        ids: set[str] = {n.id for n in nodes}
        succ: dict[str, list[str]] = defaultdict(list)
        for n in nodes:
            for pred_id in n.uses:
                if pred_id in ids:
                    succ[pred_id].append(n.id)

        for n in nodes:
            seen: set[str] = set()
            stack = list(succ.get(n.id, []))
            while stack:
                x = stack.pop()
                if x not in seen:
                    seen.add(x)
                    stack.extend(succ.get(x, []))
            n.descendant_count = len(seen)

    @staticmethod
    def _compute_metrics(nodes: list[GraphNode]) -> None:
        """
        Compute cumulative costs for every node.

        ``effort_local`` is the work remaining to formalise the node:

        - ``0``                         if it is already formalised in Lean,
                                        or marked ``\\mathlibok`` (it exists in
                                        mathlib, so there is nothing to write)
        - for proof nodes (theorem/lemma/…): the draft proof's tex size,
          or ``None`` (∞) when there is no proof to estimate from
        - for definitions and other non-proof nodes: the tex size of the
          declaration's *content* (its statement) — the proof is irrelevant

        Other metrics:

        - ``proof_size_tex_total``  = Σ proof_size_tex  over {v} ∪ ancestors(v)
        - ``proof_size_lean_total`` = Σ proof_size_lean over {v} ∪ ancestors(v)
        - ``effort_total``          = Σ effort_local    over {v} ∪ ancestors(v)

        Any sum containing a ``None`` term evaluates to ``None`` (representing ∞).
        """
        uses_of  = {n.id: n.uses            for n in nodes}
        rel_tex  = {n.id: n.proof_size_tex  for n in nodes}
        rel_lean = {n.id: n.proof_size_lean for n in nodes}

        def ancestors_incl(node_id: str) -> set[str]:
            visited: set[str] = {node_id}
            queue = list(uses_of.get(node_id, []))
            while queue:
                curr = queue.pop()
                if curr not in visited:
                    visited.add(curr)
                    queue.extend(uses_of.get(curr, []))
            return visited

        def sum_opt(ids: set[str], rel: dict[str, Optional[int]]) -> Optional[int]:
            total = 0
            for uid in ids:
                c = rel.get(uid)
                if c is None:
                    return None
                total += c
            return total

        def effort_local(n: GraphNode) -> Optional[int]:
            # Already formalised in Lean, or available in mathlib → nothing to do.
            if n.mathlib_ok or n.proof_size_lean is not None:
                return 0
            # Proof nodes: work ≈ the draft proof; ∞ when there is no draft.
            if n.type in _PROOF_TYPES:
                return n.proof_size_tex
            # Definitions &c.: work ≈ the content of the declaration itself.
            return DAG._count_tex_chars(n.statement)

        rel_effort = {n.id: effort_local(n) for n in nodes}

        for n in nodes:
            anc = ancestors_incl(n.id)
            n.effort_local          = rel_effort[n.id]
            n.proof_size_tex_total  = sum_opt(anc, rel_tex)
            n.proof_size_lean_total = sum_opt(anc, rel_lean)
            n.effort_total          = sum_opt(anc, rel_effort)

    @staticmethod
    def _build_edges(nodes: list[GraphNode]) -> list[Edge]:
        ids: set[str] = {n.id for n in nodes}
        return [
            Edge(source=pred_id, target=n.id)
            for n in nodes
            for pred_id in n.uses
            if pred_id in ids
        ]

    @staticmethod
    def _count_tex_chars(proof: str) -> Optional[int]:
        """Character count of *proof* after stripping LaTeX % comments."""
        if not proof:
            return None
        out, i, n = [], 0, len(proof)
        while i < n:
            if proof[i] == '\\' and i + 1 < n:
                out.append(proof[i:i+2])
                i += 2
            elif proof[i] == '%':
                while i < n and proof[i] != '\n':
                    i += 1
            else:
                out.append(proof[i])
                i += 1
        stripped = ''.join(out).strip()
        return len(stripped) if stripped else None
