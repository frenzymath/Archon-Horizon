---
name: blueprint-integrity
description: >-
  Audit blueprint structure and traceability: source anchors, declaration
  labels, Lean attachments, proof ownership, dependency edges, and honest node
  status.
---

# Blueprint integrity

Review a bounded chapter or node set using [[blueprint-conventions]] and
[[hgraph]]. Keep the blueprint as pure mathematics and treat its annotations as
claims that need independent evidence. The conventions are advisory: first
identify the project's established label, title, provenance, and proof-metadata
dialect, then check consistency with that dialect rather than enforcing a
Horizon default mechanically.

## Structural checks

Inspect whether:

- every source-derived block has a real `\source{...}` or project-supported
  `\dcref{...}` anchor that was read;
- local `\label` ids are stable and preferably semantic, and environment titles
  are reader-facing rather than opaque ids, task numbers, or status markers;
- `\dcref` values identify the cited source/document convention, while
  `\label` identifies a local DAG node and `\uses` identifies a local edge;
  verify that `\dcref` points to a real source reference and is not being used
  as a substitute for a local dependency;
- `\lean{...}` names declarations whose signatures match the statement;
- `\leanok`, `\mathlibok`, and `\notready` express the actual state, without
  rewarding a helper or a weaker specialization;
- `\uses{...}` edges cover statement dependencies and proof-only dependencies
  in the right place;
- a proof is attached to its own statement, with `\proves` where the format
  needs it, and the named declaration is the terminal source-shaped result;
- pure mathematical prose contains no tactic logs, project history, or hidden
  implementation assumptions.

Check macro placement as well as macro content. By Horizon's preferred style,
`\label`, `\dcref`/`\source`, `\lean`, and status markers live on the statement,
and the following `proof` contains no annotation macros. If the project dialect
intentionally permits proof-side `\uses` or `\proves`, verify that they are
limited to proof dependency/attachment metadata and do not carry source,
statement, or status claims. Do not flag an intentional local convention merely
for differing from the preferred style; do flag inconsistent placement that
causes metadata to be dropped or misattached by the parser.

Run graph sync for the scoped project and read every warning. Check the node's
actual consumers and dependency cone; a green parser or nonempty `\lean` list
does not establish `\leanok`.

## Report

Give the node/chapter, exact macro or declaration, source location, and a
minimal correction. Separate a source error, a blueprint error, a Lean mismatch,
and generated graph churn. Respect frozen paths: file an issue instead of
silently rewriting them. Pair with [[source-fidelity]] for mathematical shape
and [[graph-traceability]] for workspace-wide closure/status questions.
