---
name: lean-proof-repair
description: Diagnose a failing Lean proof or build using actual goal states and compiler errors; useful for type mismatches, instance failures, stale imports, and elaboration timeouts.
metadata:
  category: lean
license: MIT
---

# Lean Proof Repair

Repair the first meaningful diagnostic with a concrete explanation of why the
change should fix it. A failing elaboration is evidence about the attempted
formal proof, not evidence that the intended theorem should be weakened.

Read the actual declaration, its local context, and the toolchain revision.
Prefer LSP diagnostics and goal inspection during edits when available. Without
them, use the project's supported file/module check through the managed build
helper described in `horizon-formalization`. Build changed imported modules
before trusting a check that can read their older compiled artifacts.

Use a short feedback loop:

1. Capture the first causal error and the goal/hypotheses at that location.
   Later diagnostics may be consequences of it.
2. Classify the issue: unavailable name/import, wrong application, representation
   mismatch, instance synthesis, a missing mathematical argument, resource
   contention, or expensive elaboration. See [diagnostics](references/diagnostics.md)
   for the relevant branch.
3. Make one targeted change and recheck. For a missing API, load `lean-search`.
   For a mathematical gap, write the needed intermediate statement and its
   argument before trying unrelated tactics.
4. Compare the new diagnostic with the previous one. If the same blocker
   persists without new information, inspect the types, minimize the example,
   or change the argument. Do not resample the same proof indefinitely.
5. Check the repaired declaration's consumers and final build boundary. Keep
   a useful partial proof or diagnostic with its exact source revision when a
   dependency prevents completion.

Temporary diagnostic code belongs in the assigned workspace's scratch area,
not the library's permanent public modules. Remove temporary debug options and
failed proof branches from the published change. Do not replace proof gaps with
new axioms, assumed certificates, hidden admissions, or stronger hypotheses.
If evidence shows the statement itself is wrong, explain the mismatch and use
the assignment's statement-repair/review authority, including affected users.

A `sorry` search is a useful warning scan, not a proof audit. At publication,
inspect `#print axioms` for the actual public endpoint and relevant dependency
closure, and compare the result with the project's trust policy. A successful
build proves neither source alignment nor absence of a hidden assumption.
Record remaining admissions honestly; their acceptability depends on the
destination's phase and policy, not this skill.

For recurring simplifier trouble use
[lean-simplifier](../lean-simplifier/SKILL.md); for measured expensive
elaboration use [lean-performance](../lean-performance/SKILL.md). If the
same API problem affects independent consumers, investigate
[definition-quality](../../review/definition-quality/SKILL.md) instead of
adding another local transport workaround.

Adapted from [lean4-skills](../../_sources/lean4-skills/PROVENANCE.md).
