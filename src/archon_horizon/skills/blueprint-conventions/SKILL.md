---
name: blueprint-conventions
description: The LaTeX-subset blueprint format and house style — environments, \uses/\lean/\leanok/\mathlibok/\notready, the math-purity rule, and complete (not sketched) proofs.
---

Blueprints are a LaTeX subset (the `leanblueprint` dialect) parsed into the DAG.
Write timeless mathematics — a standalone document, not a project journal.

## Environments

Each declaration is an amsthm-style environment; the recognized ones are
`theorem`, `lemma`, `proposition`, `corollary`, `definition`, `remark`,
`example`, `notation`, `convention`, and `proof`. A statement environment plus
its following `proof` environment form one node's content.

## Per-node macros

- `\label{id}` — a stable id (the DAG node key).
- `\lean{Name}` — the Lean declaration(s) this node formalizes; comma-separate to
  bind several (`\lean{a, b}`) when they jointly formalize the node's statement.
  Each named declaration's signature must match the statement. The mapping need
  not be 1-to-1: a node may name several declarations, and Lean may carry
  auxiliary declarations with no node at all.
- `\uses{a,b}` — dependencies on other nodes' labels; these drive the DAG edges.
  Add an edge instead of re-explaining a dependency in prose. Put `\uses` in the
  statement for definitional deps and in the `proof` for deps used only in the
  proof.
- `\source{slug:page-0001}` — reference anchors for UI popups and audit trails.
  Use page-level ids from `references/<slug>/tex/page-0001.tex` or an equivalent
  manifest entry. Multiple anchors may be comma-separated.
- `\leanok` — the Lean is written and checked. Honest only when its Lean is
  actually checked *and* its dependencies are too. On a `proof`, it means the
  proof is formalized; on a statement, the signature is formalized.
- `\mathlibok` — the declaration is already available in mathlib (no
  formalization needed; treat as a leaf).

## House style

- **Pure mathematics only.** No Lean tactics, typeclass/implementation notes, or
  semi-Lean pseudocode. No project history ("since iter N", "our failed route")
  and no conversational filler. Not every Lean helper needs its own node —
  auxiliary or implementation-only declarations can stay unnoded; add a node when
  the result is mathematically meaningful in its own right.
- **Complete proofs, not sketches.** A node's `proof` is a real, rigorous
  mathematical proof a reader could check — not a hand-wave or a TODO. The way to
  keep it short is to *split*, not to abbreviate: if a proof is long or a node
  grows past a short paragraph, break out the hard step as its own `\uses`-linked
  lemma (down to sentence-sized lemmas if needed) so each node stays small and
  its proof stays complete.
- **One statement's worth per node.** Keep each node to a single mathematical
  statement; don't restate Lean source as prose.
- **Cite by source link, not quote comments.** Add `\source{...}` to the
  relevant node. Keep the retrieved/transcribed page text in `references/` (for
  example `references/<slug>/tex/page-0042.tex`) and let the UI expose it; do not
  paste verbatim source quotes into `% QUOTE` comments inside the blueprint.
