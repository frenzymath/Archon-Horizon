---
name: lean-cycle
description: Sustain autonomous Lean proof work through search, small edits, checking, evidence-driven replanning, and durable partial progress when a proof stalls.
metadata:
  category: lean
license: MIT
---

# Productive Proof Cycles

Start from the exact declaration, useful prior attempts, pinned dependencies,
and remaining mission obligations. Search with
[lean-search](../lean-search/SKILL.md) before inventing infrastructure. A proof
task preserves its statement; a synthesis task may refine candidate statements
while keeping the intended mathematical claim explicit and reviewed.

Choose a mathematical route, make a small edit, and inspect the resulting
goal/diagnostic. Test a meaningful hypothesis about the failure. When the goal
changes, preserve the useful intermediate result. Use
[lean-proof-repair](../lean-proof-repair/SKILL.md) for elaboration and tactic
failures and [lean-check](../lean-check/SKILL.md) at build boundaries.

If several attempts fail for the same reason, replan rather than varying tactic
syntax indefinitely. Distinguish a missing lemma from a representation mismatch,
false statement, unproved producer, or resource limitation. Read actual
definitions and library consumers. Use
[lean-counterexample](../lean-counterexample/SKILL.md) when evidence suggests
the statement may be false, and [definition-quality](../../review/definition-quality/SKILL.md)
when many consumers share the same representation problem.

A useful cycle ends with new checked source, a narrower obstacle, a disproved
route, or a concrete dependency. It does not need an arbitrary fixed number of
tactic calls or a reviewer every few minutes. Keep expensive review proportional
to changed public statements and the destination repository policy.

When an independent prerequisite blocks this route, delegate a narrower task
with its exact statement, known attempts, inputs, and integration point after
checking current ownership. Continue reachable useful work. A duplicate of the
parent goal does not handle it. A discussion without an owner and durable
follow-up is not delegation.

Preserve source and concise evidence before yielding. Reconcile open obligations
through the Horizon ledger, including justified replacements or precise future
work. Continue the retained session when asked to continue; do not restart
from zero or reload the full catalog. Stop naturally when the assignment is
handled or a real execution limit/blocker leaves no useful work, without
misrepresenting partial progress as complete.

Adapted from upstream proof-cycle guidance with Horizon's continuation,
publication, and scheduling contracts; see
[provenance](../../_sources/lean4-skills/PROVENANCE.md).
