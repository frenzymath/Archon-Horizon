---
name: blueprint-conventions
description: >-
  The LaTeX-subset blueprint format and house style — environments, semantic
  labels, bibliography plus \dcref/\source provenance, how the blueprint adapts
  or differs from sources, \uses/\lean status, math-pure complete proofs, chosen
  formalization route, and blueprint-first refactor when strategy changes.
---

Blueprints are a LaTeX subset (the `leanblueprint` dialect) parsed into the DAG.
The conventions below are Horizon's defaults and advice, not a rigid contract:
if a project, source, or established blueprint dialect uses a different
convention, follow that convention when it is intentional and parser-supported.
Record an ambiguity for the lead or reviewer instead of silently normalizing a
working project to these defaults. In every dialect, keep the distinction
between reader-facing mathematics and machine-facing metadata visible.

Write timeless mathematics — a standalone document, not a project journal.

## Blueprint is the first source of truth

- **Missing or stub is unfinished orientation.** If the project has no
  `blueprint/` directory, or only a title/skeleton with no statements, sketched
  proofs, or no `\uses` cone for the work in hand, **write a complete
  mathematical route first** — statements, rigorous proofs, `\uses`, and
  bibliography/`\dcref`/`\source` covering at least the source-facing objective
  and its next producers. Do not treat an empty graph or a thin chapter as
  permission to invent the route only in Lean.
- **Author and fix the blueprint before (or as) you formalize.** Lean implements
  the blueprint route; it must not invent a competing route while the `.tex`
  still describes another. When strategy changes, **refactor the blueprint
  first** (statements, proofs, `\uses`, chapter splits), then align Lean.
  After each coherent Lean change that affects the math path, update the
  corresponding nodes in the same session so the written route stays live.
- **Proofs are complete, not sketches.** A node's `proof` is a rigorous argument
  a mathematician could check. Missing steps, "similar to …", or "by the usual
  argument" without the argument are defects — *split* hard steps into
  `\uses`-linked lemmas rather than abbreviating.
- **The written route is the chosen route.** Do not leave alternate proof
  sketches, discarded approaches, or "one could also …" paths that Lean does
  not follow. If a reference proof is adapted, say how (below); if the project
  deliberately differs, say that explicitly and keep a single coherent path.
- Prefer a clean blueprint rewrite over patching contradictory paragraphs when
  the route pivots ([[restart-module]] applies to blueprint files too).

## Environments

Each declaration is an amsthm-style environment; the recognized ones are
`theorem`, `lemma`, `proposition`, `corollary`, `definition`, `remark`,
`example`, `notation`, `convention`, and `proof`. A statement environment plus
its following `proof` environment form one node's content.

## Per-node macros

- `\label{id}` - a stable local id (the DAG node key). Prefer a semantic,
  reader-recognizable id such as `def:exp-map` or `thm:hopf-rinow` over a bare
  sequence number, file position, task id, or implementation name. Keep an id
  stable once it has consumers; a semantic rename is a migration, not a
  cosmetic cleanup.
- `\dcref{ref}` - a source/document reference used by blueprint dialects that
  identify the cited definition, theorem, equation, or passage in the original
  work. It is provenance, not a local node id and not a dependency edge. Use
  the exact identifier supported by the project's source convention; do not
  invent a page or theorem number.
- `\source{slug:page-0001}` — Horizon reference-library anchors for UI popups
  and audit trails (see [[references]]). Use page-level ids from
  `references/<slug>/tex/page-0001.tex` or an equivalent manifest entry.
  Multiple anchors may be comma-separated. A project may use `\dcref`,
  `\source`, or both when each carries distinct information; do not mix them
  casually without a project convention.
- `\lean{Name}` — the Lean declaration(s) this node formalizes; comma-separate to
  bind several (`\lean{a, b}`) when they jointly formalize the node's statement.
  Each named declaration's signature must match the statement. The mapping need
  not be 1-to-1: a node may name several declarations, and Lean may carry
  auxiliary declarations with no node at all.
- `\uses{a,b}` — dependencies on other nodes' labels; these drive the DAG edges.
  Add an edge instead of re-explaining a dependency in prose. Prefer putting
  `\uses` on the statement; a proof-side `\uses` is a compatibility exception
  for a project dialect that records dependencies used only in that proof.
- `\leanok` — the Lean declaration and its dependency cone are written and
  checked. Prefer this marker on the statement, where it describes the node's
  formalization as a whole.
- `\mathlibok` — the declaration is already available in mathlib (no
  formalization needed; treat as a leaf). Prefer this marker on the statement.
- `\notready` — the Lean is **not written yet**: the node's statement stands but
  no declaration formalizes it. Mutually exclusive with `\leanok` (a node is
  never both) — remove `\notready` exactly when you add `\leanok`, i.e. the
  moment its Lean is written and checked. It is a human-facing marker of an
  intentional gap, not a DAG edge. Existing dialects may attach a status marker
  after a proof; preserve that convention only when the parser and local style
  define its association unambiguously.

Put node metadata on the **statement**. In particular, `\label`, `\dcref` or
`\source`, `\lean`, and statement-status markers describe the declaration before
its proof and should not be repeated in the following `proof` environment. A
proof should contain ordinary mathematical prose and displayed mathematics,
not project-history or annotation macros. Some existing blueprint dialects use
proof-side `\uses{...}` or `\proves{...}` to record dependencies that occur only
in a proof; retain those only when that project's convention and parser require
them, and do not mistake them for source or statement metadata. If a project
adopts the stricter no-macro proof convention, keep proof dependencies in the
statement or graph metadata according to its established format.

An optional environment title should also be semantic and reader-facing:
`\begin{theorem}[Hopf-Rinow equivalence]`, not `[thm:17]`, `[TODO]`, or a
task-specific status. Use the source's title when it is meaningful, and adapt
the wording when the local blueprint has a documented naming convention. The
title is display prose; `\label` remains the stable machine key.

## Bibliography and how the blueprint uses sources

A high-quality blueprint makes **literature dependence explicit**:

1. **Bibliography.** Maintain a project `.bib` (often `blueprint/src/refs.bib`
   or similar) and cite with the project's cite command (`\cite{...}` /
   leanblueprint equivalents). Every external work the chapter relies on should
   resolve in that bibliography.
2. **Precise anchors.** Prefer `\dcref{...}` and/or `\source{...}` on the node
   for the exact definition, theorem, equation, or page — not only a book-level
   cite. Read the source before anchoring ([[references]]).
3. **Adaptation notes (mathematical, not journal).** Where the blueprint
   follows a reference closely, a short remark or the proof prose may state
   *which* result is used. Where it **differs** — different hypotheses, a
   reordered lemma chain, a finite/model proxy, a gap filled locally, or a
   route chosen over the paper's — say so in timeless mathematical language
   (e.g. a `remark` environment or one clear sentence in the proof). Do not
   write session history ("after iter 12 we switched"); write the mathematical
   relationship to the source.
4. **No silent drift.** If Lean implements a weaker/stronger statement than the
   cited source, the blueprint statement must match the Lean contract and the
   difference from the source must be visible on the node.

## House style (advisory)

- **Pure mathematics only.** No Lean tactics, typeclass/implementation notes, or
  semi-Lean pseudocode. No project history ("since iter N", "our failed route")
  and no conversational filler. Not every Lean helper needs its own node —
  auxiliary or implementation-only declarations can stay unnoded; add a node when
  the result is mathematically meaningful in its own right. In particular, never
  add a `Formalization note` paragraph to blueprint `.tex`: attach implementation
  details, failed approaches, and mechanization caveats to the statement's hgraph
  node with `horizon graph ... add comment` instead.
- **Complete proofs, not sketches.** Keep proofs short by *splitting*, not by
  omitting detail: if a proof is long or a node grows past a short paragraph,
  break out the hard step as its own `\uses`-linked lemma (down to sentence-sized
  lemmas if needed) so each node stays small and its proof stays complete.
- **One statement's worth per node.** Keep each node to a single mathematical
  statement; don't restate Lean source as prose.
- **Cite by bibliography + structural anchors, not quote dumps.** Keep retrieved
  page text in `references/`; do not paste verbatim source quotes into `% QUOTE`
  comments inside the blueprint.

When a node is source-backed and its Lean correspondence is being written or
changed, use [[formalization-review]] as an optional semantic aid. It helps
compare the actual declaration type, hypotheses, domains, attachments, and
bridges without requiring every node to receive the same depth of review.

When a project uses different labels, titles, provenance macros, or proof-side
dependency conventions, [[blueprint-integrity]] should check that local scheme
for consistency rather than marking it wrong merely because it differs from
these defaults.
