---
name: lean-tactics
description: Choose Lean proof strategies from the actual goal shape, normalize representations, and use domain solvers with the hypotheses they require.
metadata:
  category: lean
license: MIT
---

# Goal-Directed Tactics

Inspect the actual goal and local hypotheses first. Search for an existing
theorem before constructing a large tactic proof. Read
[strategy patterns](references/strategy-patterns.md) when choosing between
algebraic normalization, order reasoning, congruence, extensionality, or
measure/topology APIs. Candidate tactic availability depends on the pinned
Lean/mathlib version and imports.

Separate structural steps from mathematical content. Introduce quantified
variables, destructure witnesses, and provide the intended intermediate fact.
An arithmetic solver cannot discover an omitted continuity theorem, and a
simplifier cannot manufacture an absent nonzero assumption. State the missing
fact explicitly and find or prove it.

After rewriting or simplification, inspect the new goal before constructing a
`calc` chain. Its initial expression must match the current goal, not a
remembered expression from before normalization. Use typed intermediate steps
where inference is ambiguous or costly.

Use exploratory tactics to learn an available proof, then retain the useful
stable suggestion when appropriate. Do not run an indiscriminate list of
tactics repeatedly. When evidence stops changing, use
[lean-cycle](../lean-cycle/SKILL.md) to replan and
[lean-proof-repair](../lean-proof-repair/SKILL.md) for diagnostics.

Adapted from upstream `tactic-patterns.md`, `calc-patterns.md`, and
`proof-simplification.md`; [provenance](../../_sources/lean4-skills/PROVENANCE.md).
