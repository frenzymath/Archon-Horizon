---
name: lean-counterexample
description: Investigate a suspected false Lean or informal statement with small witnesses, boundary cases, and certified refutations while preserving the original target.
metadata:
  category: lean
license: MIT
---

# Counterexample Investigation

Fix the exact target and its definitions, including implicit instances and
quantifier dependencies. Search prior attempts and the source for a missing
side condition. A failed proof attempt is not evidence that the statement is
false.

Choose a separating case: empty/singleton domains, zero/unit values, a boundary
point, a noninjective map, a smallest finite model, or a known obstruction from
the relevant mathematics. Verify that the witness satisfies *all* hypotheses.
Pay attention to totalized division, natural subtraction, vacuous quantifiers,
and local versus uniform constants.

Use computation to explore bounded examples when available, but distinguish an
observed value, a candidate mathematical witness, and a Lean-checked negation.
For universal claims, an explicit witness plus a proof it violates the
conclusion can certify refutation. For an implication, establish the premises
as well as failure of the conclusion. A finite search with no witness proves
nothing about the unsearched infinite domain.

Keep the original theorem unchanged during investigation. Add a separately
named counterexample or scratch theorem so the failed contract remains clear.
Avoid circular proofs whose negation uses the original admitted theorem or an
inconsistent environment. Inspect the refutation's axioms and check it with
[lean-check](../lean-check/SKILL.md).
Claim certification only when the negation and its dependency closure contain
no admissions and satisfy the project's trust policy at the checked revision.

Report one of: certified refutation at an exact revision; concrete witness with
an unverified formal step; or inconclusive search with its domain and evidence.
Do not weaken the original theorem and call that its proof. If a correction is
needed, record why and update the mathematical mission/roadmap through the
configured workflow. Use [statement-alignment](../../review/statement-alignment/SKILL.md)
to compare the corrected contract to the source.

Adapted from upstream counterexample workflow principles, without its
interactive command menus or artifact runtime;
[provenance](../../_sources/lean4-skills/PROVENANCE.md).
