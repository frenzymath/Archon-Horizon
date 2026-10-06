---
name: lean-simplifier
description: Debug Lean simp behavior and design local or global normalization rules without rewrite loops, conflicting normal forms, or unnecessary automation.
metadata:
  category: lean
license: MIT
---

# Simplification And Normal Forms

Inspect the actual expression and desired normal form. Distinguish a directed
one-off rewrite (`rw`), local normalization (`simp only`), and terminal closure
using the project's normal simp set (`simp`). A globally true equality is not
necessarily a good global rewrite.

Use `simp?` where available to inspect a useful lemma set. Reduce the active
set with `simp only [...]` or exclude a suspect lemma. Trace the simplifier only
after narrowing the problem; broad traces can obscure the actual conflict.
If normalization succeeds but a mathematical goal remains, use an appropriate
theorem or solver rather than adding more rewrite rules.

Read [rewrite policy](references/rewrite-policy.md) before adding `@[simp]`, a
custom simp set, or a simproc. Test the left side with the intended simp setup
*excluding the proposed lemma*, and ensure the right side progresses toward
the intended normal form without a cycle. Validate the rule on representative
consumers, not only the proof that requested it.

Prefer the least broad mechanism that meets real use. A local lemma does not
need a global attribute simply because one proof benefits. Conversely, a
canonical projection/cancellation rule used throughout an API often belongs
in its simp interface. Preserve established destination conventions.

Adapted from upstream `simp-reference.md` at the
[pinned revision](../../_sources/lean4-skills/PROVENANCE.md).
