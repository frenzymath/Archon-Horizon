---
name: hgraph
description: Per-node progress on the dependency graph — hgraph keeps one file per statement/Lean declaration under <project>/hgraph/, agents attach comments (failure memory) and Maths/Lean reviews to nodes, and `hgraph frontier` ranks what to prove next. Preferred over raw leandag queries when hgraph is installed.
---

Each project may carry an **hgraph**: a plain-files semantic graph under
`<project>/hgraph/` — one Markdown node per blueprint statement and per Lean
declaration, typed edges (`uses` = the hard dependency DAG, `formalizes` =
informal↔formal identity). `hgraph sync` (run automatically at run boundaries)
reconciles the blueprint + Lean sources into it; your **authored** additions
(comments, reviews, metadata) are never touched by sync. The files are
committed to the workspace ledger like everything else.

Run the CLI from the project directory (or pass the project path as the last
argument). Address nodes by `label:<latex-label>` or `decl:<lean-fqname>` —
raw ids are opaque hashes. Most commands accept `--json`.

## Read

- `hgraph list --state ready` — nodes whose dependencies are all closed:
  provable *now*. Other states: `closed`, `blocked`, `formalized_open`.
- `hgraph frontier` — the ready nodes ranked by how many downstream nodes a
  proof would unlock. **Use this when picking the next node to prove.**
- `hgraph get label:thm:foo` — one node: statement, `lean_status`
  (`lean_ok | mathlib_ok | sorry | empty`, derived from scanning the Lean, not
  from `\leanok`), deps, attached comments/reviews.
- `hgraph ancestors label:thm:foo` — the dependency cone.
- `hgraph stats` — per-state counts for the project.

## Write — per-node progress lives WITH the node

- **Comment = failure memory / progress note on that node.** When an approach
  fails or you learn something node-specific, record it where the next session
  will look first:
  `hgraph add comment label:thm:foo --author agent --content "tried simp+ring, fails because …"`
- **Review = a Maths / Lean verdict** (independent axes, `good|bad`):
  `hgraph add review label:thm:foo --maths good --lean bad --lean-comment "statement ok; proof has a sorry at the succ case"`
- **Metadata**: `hgraph modify node label:thm:foo --set status=verified`

Prefer node comments over inbox items for anything scoped to ONE node; use the
inbox (skill: `horizon-inbox`) for cross-node or cross-session coordination.

## Relation to the blueprint DAG

The dashboard's blueprint DAG is generated from the hgraph (with a plain
LaTeX-parser fallback when hgraph is unavailable) — so keeping the blueprint
`\uses{}`/`\lean{}` annotations correct (skill: `blueprint-conventions`) is
what keeps this graph correct. `horizon leandag` still answers cone/dependency
queries from the cached DAG JSON (skill: `leandag`).
