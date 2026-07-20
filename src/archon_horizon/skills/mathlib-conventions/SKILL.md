---
name: mathlib-conventions
description: Mathlib house style for the Lean you write — naming (snake_case props / UpperCamelCase types / lowerCamelCase defs, Is-prefix, le/lt), meaningful discoverable names, preferred abstractions, and how to add a missing lemma as reusable infrastructure instead of a local hack.
---

Write Lean that looks like it belongs in mathlib: discoverable, reusable, and
composing with the existing API. This is the style side; use [[leansearch]] to
*find* lemmas and [[lean-check]] to verify, and keep the blueprint in step via
[[blueprint-conventions]].

## Naming

- **Theorems / lemmas / `Prop`s → `snake_case`**, named by *what the statement
  says*, composing the pieces in order: `add_comm`, `mul_le_mul_left`,
  `isOpen_iUnion`, `continuous_id`. A reader should reconstruct the statement
  from the name.
- **Types, structures, classes, inductives → `UpperCamelCase`**: `LinearMap`,
  `IsLindelof`, `RiemannianMetric`.
- **Definitions / terms / functions → `lowerCamelCase`**: `leviCivita`,
  `koszulRHS`, `parallelTransport`.
- **Structural predicates take the `Is` prefix**: `IsOpen`, `IsCompact`,
  `IsSymmetric`.
- **Name order relations by the operator you wrote**, not its mirror: use `le`
  / `lt` (`le_of_lt`, `add_lt_add`), not `ge` / `gt`.
- **Meaningful names only.** No `helper1`, `foo_aux`, `my_lemma`, `lemma2`.
  Ad-hoc names are invisible to search and un-reusable — the whole point of a
  mathlib-style name is that the next session (or mathlib itself) can find it.

## Search before you prove, prefer before you build

- Your "obvious lemma" is very likely already in mathlib under a more general
  name. Search first ([[leansearch]]); a negative result after a precise query
  means it's genuinely absent — then prove it, don't keep rephrasing.
- **Choose the reusable abstraction**, so your work composes with mathlib rather
  than sitting in a private silo: `Filter`/`Tendsto` over hand-rolled ε–δ, the
  Bochner `MeasureTheory.integral` over an ad-hoc integral, bundled morphisms
  (`…Hom`) and typeclasses over unbundled functions-plus-hypotheses, unless the
  problem is genuinely about the lower-level object.

## Missing infrastructure → build it properly, don't defer or hack

When a needed fact truly isn't in mathlib, the failure mode is to leave a
`sorry` "waiting for an upstream PR", or to inline a brittle one-off. Instead:

- **State the missing lemma in mathlib-ready generality**, with a proper name and
  a *real* proof attempt (a partial proof beats a documented gap), as
  project-local infrastructure others can reuse.
- **Factor hard steps into their own named, reusable lemmas** rather than a
  monolithic proof — mirroring the blueprint's "split, don't abbreviate" rule.
- **Record it** so it isn't an invisible dependency: give the new declaration a
  blueprint node (`\lean{…}` + `\uses{…}`) and note it for the roadmap, so future
  sessions and the DAG can see the new frontier ([[blueprint-conventions]], [[hgraph]]).

## House style (light touch)

- Module docstring at the top; group related material into `section`s /
  `namespace`s; keep lines readable (~100 cols); align `calc` steps.
- Prefer `simp`/`grind`-normal statements so lemmas fire as `@[simp]` candidates
  and rewrites compose.
- Comments explain the *mathematics*, not the development history ("was a sorry",
  "TODO next session" — keep that in the report/inbox, not the source).
- Mark reusable facts `@[simp]` only when they're genuinely confluent rewrites.

## Faithfulness (non-negotiable)

- **Never weaken a statement to make it pass**, and never change a declaration's
  statement to dodge its proof. A proof that games the checker is a failure.
- **No `axiom` / `admit` / `native_decide` on a goal you haven't honestly
  closed**, and no `sorry` reported as done — see [[lean-check]] for verifying a
  build is genuinely sorry-free.
