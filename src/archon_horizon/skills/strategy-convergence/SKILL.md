---
name: strategy-convergence
description: Independently audit whether a formalization route is mathematically valid and converging, with explicit prerequisites, frontier choices, stopping reasons, and distinction between finite proxies and headline results.
---

# Strategy and convergence

Use this lens for a chapter, milestone, or multi-session task. Reconstruct the
route from the source, roadmap, graph frontier, reports, and actual commits;
do not equate many edits with progress.

## Audit the route

- State the source-level result and the exact Lean/blueprint endpoint.
- List prerequisites and identify a real producer for each, including analytic,
  geometric, algebraic, and regularity assumptions.
- Check that proposed detours use the hypotheses and structures they claim to
  replace; a valid proof of a neighboring theorem may not discharge the target.
- Distinguish a finite, conditional, or model result from the asymptotic,
  intrinsic, or algorithmic headline result.
- Compare repeated attempts: did the frontier move, did a consumer appear, did
  an assumption get exposed, or did only wrappers/comments/graph timestamps
  change?
- Identify the smallest next obligation that unlocks meaningful downstream
  work, and a concrete reason to stop when it is not attainable.
- Check that the **roadmap tree itself** matches the reconstructed route. An
  empty roadmap, a flat list that never grew children, or a frozen outline whose
  status flips while the math route changed are strategy defects: the lead agent
  should `roadmap add` / `set --parent` / `remove` / `rename` so the board is the
  plan, not a stale label list.
- Check that the **blueprint** still describes the chosen route (complete proofs,
  no abandoned alternate paths, literature adaptation noted). When strategy
  pivots, the blueprint must be refactored so it remains the first source before
  Lean ([[blueprint-conventions]]).

## Calibrate the verdict

Use `converging`, `partially advancing`, `churning`, or `stuck`, with evidence
for the label. A blocked route is not a failure if the blocker is made precise;
an unproved conditional adapter is not the headline theorem. Flag impossible
or source-incoherent plans rather than encouraging more retries.

Report the highest-value next action, unresolved decisions, and non-goals. Pair
with [[load-bearing]], [[source-fidelity]], and [[verification-evidence]].
