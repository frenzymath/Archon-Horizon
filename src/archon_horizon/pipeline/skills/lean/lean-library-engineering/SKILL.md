---
name: lean-library-engineering
description: Improve reusable Lean library APIs, definitions, names, proof structure, compilation performance, and repository organization during extraction or focused engineering work.
metadata:
  category: lean
license: MIT
---

# Lean Library Engineering

Use for library extraction, refactoring, or an engineering review. Routine
workspace proof work does not need to satisfy the full library publication
standard before another worker can use it. The destination policy and current
assignment determine the required rigor and whether this session edits or only
reviews.

Start at the exported result. Read its intended mathematics and important
consumers, then work backward through the definitions and dependencies it needs.
Compare against the destination's existing API before moving whole workspace
directories into it. Use `lean-search` when an existing abstraction or theorem
may replace custom infrastructure.

Investigate in this order when relevant:

1. **Meaning and reuse.** Does the statement express the intended result with
   natural hypotheses? Can a definition use an established interface? Remove
   cheap accidental specialization without inventing a hierarchy for imagined
   users. New structures should package meaningful shared data or laws, not
   force callers through wrappers created only for this proof.
2. **Public API.** Choose names, namespaces, parameters, and file placement that
   a library user can predict. Confirm useful operations, extensionality facts,
   simplification behavior, and instances by trying a representative consumer.
3. **Proof argument.** Look for a direct existing theorem or simpler mathematical
   route before shortening tactics. Extract a helper for reuse or a clear
   mathematical boundary; keeping a local `have` can be better than exporting
   another declaration. A smaller line count alone is not an improvement.
4. **Cost and stability.** Measure actual slow declarations and import costs.
   Narrow proof search or supply explicit intermediate types when useful.
   Preserve readable, reliable automation when no problem is demonstrated.
5. **Publication fit.** Check project layout, generated imports, documentation,
   citations, CI, and toolchain compatibility against the intended destination.

Read [library quality](references/library-quality.md) for a concrete checklist
when preparing or reviewing publication. Select its applicable parts; a small
fix does not justify redesigning the repository.

Investigate foundational representations with
[definition-quality](../../review/definition-quality/SKILL.md), move consumers
coherently with [lean-refactor](../lean-refactor/SKILL.md), and use
[lean-performance](../lean-performance/SKILL.md) for measured compilation
concerns. Read [lean-simplifier](../lean-simplifier/SKILL.md) before changing
global normalization, and [lean-proof-golf](../lean-proof-golf/SKILL.md) for
local improvements to an already correct proof. For source-backed results,
check [statement-alignment](../../review/statement-alignment/SKILL.md) before
polishing a proof of the wrong type.

For review, report the exact declaration/revision, consequence for users, and a
specific repair or counterexample. Distinguish merge blockers from suggestions.
Use the pinned review workflow for Forge discussions, reviewer attribution, and
maintainer decisions. This skill does not grant write or merge permissions.

For edits, preserve the endpoint's mathematical meaning, update affected
consumers, and check through Horizon's managed build path. Keep toolchain
upgrades separate when they would obscure the extraction or need broader
validation. Document measured costs and commands, not unverified speed claims.

Adapted from [lean4-skills](../../_sources/lean4-skills/PROVENANCE.md).
