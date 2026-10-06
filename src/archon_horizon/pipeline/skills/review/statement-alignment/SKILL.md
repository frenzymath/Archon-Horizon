---
name: statement-alignment
description: Compare an exact Lean declaration with its intended mathematical statement, detecting changed quantifiers, hidden hypotheses, proxy objects, and missing equivalence bridges before expensive proof work.
metadata:
  category: review
---

# Statement Alignment

Use for source-facing theorems, foundational definitions, and changed public
statements. A routine internal helper usually needs only a direct comparison.
Alignment is useful before proving an expensive wrong target; it is not a
mandatory extra review session for every workspace edit.

Read the source passage and surrounding conventions, then fix the exact
candidate declaration: namespaces, section variables, instance parameters,
binders, hypotheses, and conclusion. A declaration name or an abbreviated
snippet cannot establish correspondence. A statement-only skeleton may contain
an explicit `sorry`; say that its proof is unfinished.

Translate the elaborated type into a standalone mathematical proposition. Make
implicit instances and meanings of project definitions explicit. Compare it to
the intended claim across objects, domains, quantifier order, boundary cases,
regularity, and conclusion strength. A model of the object needs a proved
bridge to the intrinsic object, not just a matching name.

For a high-risk milestone, use fresh independent context when available: the
translator receives the complete declaration and the definitions needed to
interpret it, without the intended theorem or prior verdict; the comparator
receives the intended statement and the translation. Missing definition bodies
make a blind translation unreliable, so include them or record that limitation.
One session may make separate passes when native subagents are unavailable.
Do not schedule a new Horizon assignment solely for ritual independence.

Read [semantic probes](references/semantic-probes.md) for concrete failure
patterns and evidence to collect. Preserve the actual contract during repair.
When the mathematics requires changing it, explain the discrepancy and update
the mission/roadmap through its configured policy, with the corresponding Lean
change. The old weakened graph statement cannot validate its own weakening.

Record the source locator, graph/PR revision, exact declaration and definitions,
translation, discrepancies or no-discrepancy finding, and unchecked scope.
Reuse this evidence when those inputs have not changed. A proof-only edit need
not trigger another translation. Alignment does not establish kernel checking
or proof closure; use [proof-review](../proof-review/SKILL.md) for that question.
