---
name: leandag
description: Consult and refresh the blueprint dependency DAG (leandag) — declaration dependencies, cones, proved status, isolated/dangling nodes, verified union/intersection across projects — via `horizon leandag` and the cached DAG files.
---

The DAG links blueprint nodes by `\uses` edges. An edge `source -> target` means
*target depends on source*. The engine records each node's `\lean` link,
`proved`/`leanok` status, effort metrics, and Lean source metadata.

## Keep It Fresh

- Run the project build after Lean/blueprint edits so generated Lean artifacts and
  imported libraries are current.
- Run `horizon blueprint` after blueprint or Lean declaration changes; it writes
  `.archon-horizon/blueprints/<project>.json` for the dashboard and cached DAG
  consumers.
- Use `horizon leandag` for live inspection when deciding what to prove next.

## `horizon leandag` (preferred)

- `horizon leandag` — per-project summary: nodes, edges, proved count, and any
  isolated nodes (no edge in or out — usually a blueprint bug to fix).
- `horizon leandag -p <project> --node <label>` — one node's direct deps, full
  transitive cone, and reverse deps.
- `horizon leandag -p a -p b --union` — merge projects by declaration/node id.
  Same-id nodes are combined; divergent non-signature metadata is tolerated, but
  a Lean signature mismatch is an error to investigate.
- `horizon leandag -p a -p b --intersect` — only shared declaration/node ids,
  with the same signature check.
- Add `--json` for machine-readable output.

A node is "ready" when all dependencies are proved. Prefer ready nodes when
choosing what to attempt next.

## Cached JSON

`horizon blueprint` writes each project's DAG to
`.archon-horizon/blueprints/<project>.json` (`nodes`, `edges`, `dangling`) for
the dashboard. The DAG is built by **hgraph** (see the `hgraph` skill), which
resolves `\uses` at sync time and reports unresolved references as sync
warnings and `dangling` entries; the plain LaTeX-parser fallback populates
`dangling` too. To check for orphan/unresolved nodes, use `horizon leandag`
(or `hgraph sync` and read its warnings) rather than relying only on `dangling`.
