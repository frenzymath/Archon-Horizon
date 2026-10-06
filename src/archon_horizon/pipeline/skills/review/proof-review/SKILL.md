---
name: proof-review
description: Audit an informal or Lean proof for mathematical contribution, source fidelity, hidden assumptions, dependency closure, and exact build evidence without confusing compilation with completion.
metadata:
  category: review
---

# Proof Review

Fix the exact claim, declaration, source locator, repository revision, and PR
head. Review against that snapshot. A prior report is a claim to verify, and
a clean source scan is not evidence for a transitive dependency closure.

Read the proof as mathematics before inspecting tactics. Trace how the stated
hypotheses produce the conclusion. Check omitted cases, circular reasoning,
finite/local proxies, and certificate fields that assume the difficult step.
An alternate proof is welcome when it establishes the same statement; matching
the paper's tactics or argument structure is unnecessary.

Use [statement-alignment](../statement-alignment/SKILL.md) for source-facing or
changed public statements, with effort proportional to the risk. Use
[definition-quality](../definition-quality/SKILL.md) where the result depends
on potentially vacuous or over-specialized representations.

Inspect important declarations with `#print axioms` using their actual fully
qualified names. Classify direct admissions, transitive admissions, custom
axioms, and the project's accepted trust assumptions separately. Standard
classical axioms are not automatically defects. A proof with only standard
axioms may still prove the wrong theorem from an assumed conclusion.

Read [trust and closure](references/trust-and-closure.md) for exact probes and
limitations. Use [lean-check](../../lean/lean-check/SKILL.md) to establish
build scope. Distinguish LSP diagnostics, single-file elaboration, named module
build, root build, and downstream compatibility. Check that the advertised
theorem is reachable from the target actually checked.

Report findings by consequence: precise location/revision, evidence, impact,
and a repair. Mark unverified possibilities as such; a review may find no
issues. For disputed findings, reproduce the smallest separating probe rather
than averaging narrative verdicts. Use the configured reviewer descriptor for
scope and [horizon-review](../horizon-review/SKILL.md) for attributed Forge
reviews. This procedure neither grants merge authority nor makes every free
workspace edit pass a strict publication review.
