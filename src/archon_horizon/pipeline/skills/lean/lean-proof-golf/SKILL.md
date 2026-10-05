---
name: lean-proof-golf
description: Simplify a working Lean proof by finding a more direct argument and reducing needless inference or duplication while preserving the statement, clarity, and checked behavior.
metadata:
  category: lean
license: MIT
---

# Improve A Working Proof

Start with a checked baseline and the exact declaration contract. Search for
an existing stronger or equivalent theorem before polishing a long custom
argument. Optimize correctness, mathematical directness, understandable
dependencies, and measured cost; use text length as a secondary criterion.

Read [proof improvement patterns](references/proof-improvement-patterns.md)
when selecting an edit. Work on one proof or coherent group, checking after
each meaningful transformation. Preserve useful intermediate facts and names.
Do not inline repeated work, hide a mathematical argument behind unexplained
automation, or add assumptions to reduce lines.

Prefer direct theorem application when it makes the reason clear. Retain a
`calc` chain when it explains a multi-step argument. Test whether an `ext`
proof is definitionally `rfl`, but do not assume equality merely from syntax.
Use local solver automation where it is the natural idiom; replacing a clear
fast solver call with many manual lines need not improve anything.

Only claim speedups from measurements using
[lean-performance](../lean-performance/SKILL.md). Simplifier tuning uses
[lean-simplifier](../lean-simplifier/SKILL.md). Check the final source through
[lean-check](../lean-check/SKILL.md), including consumers when the refactor
changes exported helpers. A failed optimization should restore only the
experiment's changes, preserving unrelated edits.

Adapted from upstream `proof-golfing.md`, `proof-golfing-patterns.md`, and
`proof-simplification.md`; [provenance](../../_sources/lean4-skills/PROVENANCE.md).
