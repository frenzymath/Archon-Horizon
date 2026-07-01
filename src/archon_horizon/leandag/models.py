from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class BlueprintDecl:
    """A mathematical declaration extracted from a leanblueprint LaTeX source."""
    id:         str
    type:       str
    title:      str
    chapter:    str
    statement:  str
    uses:       list[str]
    sources:    list[str]
    proof_tex:  str
    lean_names: list[str]   # every name in \lean{a, b}; empty if none
    is_proved:  bool
    mathlib_ok: bool = False   # \mathlibok — the result already exists in mathlib
    tex_file:   str  = ""      # .tex file this declaration was written in


@dataclass
class LeanDecl:
    """A Lean 4 declaration extracted from .lean source files."""
    name:       str
    source:     str
    proof_size: Optional[int]
    has_sorry:  bool
    file:       str = ""   # .lean file this declaration was found in


@dataclass
class GraphNode:
    """
    A node in the dependency graph, combining blueprint and Lean data.

    All metric fields are populated by :class:`DAG`; they are ``None``
    when the underlying data is absent (treated as ∞ in complexity sums).
    """
    # Identity
    id:        str
    type:      str          # lemma | theorem | definition | … | lean_aux
    title:     str
    chapter:   str
    statement: str
    uses:      list[str]    # ids of direct predecessors
    sources:   list[str]    # reference anchors from \source{...}

    # Blueprint-derived fields (absent for lean_aux nodes)
    lean_name:  Optional[str] = None   # display string; ", "-joined when \lean{} lists several
    proved:     bool          = False
    mathlib_ok: bool          = False  # \mathlibok — result already in mathlib (effort 0)
    proof_tex:  str           = ""

    # Lean-derived fields
    lean_source:     str           = ""
    proof_size_lean: Optional[int] = None
    has_sorry:       bool          = False

    # Source-file provenance (for the file-view overlay)
    tex_file:  str = ""   # .tex file the blueprint declaration was written in
    lean_file: str = ""   # .lean file the matched Lean declaration was found in

    # Graph metrics (set by DAG)
    dep_count:        int = 0   # number of direct dependencies
    rdep_count:       int = 0   # number of nodes that directly depend on this one
    descendant_count: int = 0   # number of nodes that transitively depend on this one

    # Complexity metrics (set by DAG)
    proof_size_tex:        Optional[int] = None
    effort_local:          Optional[int] = None   # 0 if proved, tex size if draft, ∞ if absent
    proof_size_tex_total:  Optional[int] = None   # cumulative over ancestor subtree
    proof_size_lean_total: Optional[int] = None
    effort_total:          Optional[int] = None

    # ── Serialisation ──────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "id":                    self.id,
            "type":                  self.type,
            "title":                 self.title,
            "chapter":               self.chapter,
            "statement":             self.statement,
            "uses":                  self.uses,
            "sources":               self.sources,
            "lean_name":             self.lean_name,
            "proved":                self.proved,
            "mathlib_ok":            self.mathlib_ok,
            "proof_tex":             self.proof_tex,
            "lean_source":           self.lean_source,
            "proof_size_lean":       self.proof_size_lean,
            "has_sorry":             self.has_sorry,
            "tex_file":              self.tex_file,
            "lean_file":             self.lean_file,
            "dep_count":             self.dep_count,
            "rdep_count":            self.rdep_count,
            "descendant_count":      self.descendant_count,
            "proof_size_tex":        self.proof_size_tex,
            "effort_local":          self.effort_local,
            "proof_size_tex_total":  self.proof_size_tex_total,
            "proof_size_lean_total": self.proof_size_lean_total,
            "effort_total":          self.effort_total,
        }

    @classmethod
    def from_dict(cls, d: dict) -> GraphNode:
        return cls(
            id        = d["id"],
            type      = d["type"],
            title     = d["title"],
            chapter   = d["chapter"],
            statement = d["statement"],
            uses      = d["uses"],
            sources   = d.get("sources", []),
            lean_name  = d.get("lean_name"),
            proved     = d.get("proved", False),
            mathlib_ok = d.get("mathlib_ok", False),
            proof_tex  = d.get("proof_tex", ""),
            lean_source     = d.get("lean_source", ""),
            proof_size_lean = d.get("proof_size_lean"),
            has_sorry       = d.get("has_sorry", False),
            tex_file        = d.get("tex_file", ""),
            lean_file       = d.get("lean_file", ""),
            dep_count  = d.get("dep_count", 0),
            rdep_count = d.get("rdep_count", 0),
            descendant_count = d.get("descendant_count", 0),
            proof_size_tex        = d.get("proof_size_tex"),
            effort_local          = d.get("effort_local"),
            proof_size_tex_total  = d.get("proof_size_tex_total"),
            proof_size_lean_total = d.get("proof_size_lean_total"),
            effort_total          = d.get("effort_total"),
        )


@dataclass
class Edge:
    source: str
    target: str
