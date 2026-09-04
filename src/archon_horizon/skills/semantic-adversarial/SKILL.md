---
name: semantic-adversarial
description: Adversarial probes for Lean predicates and theorem interfaces, aimed at vacuity, polarity, hidden assumptions, degenerate cases, and statements that are stronger or weaker than their prose.
---

# Semantic adversarial checks

Use this lens when a statement looks suspicious, has many hypotheses, or is
being used as evidence for a larger theorem. The goal is to test meaning, not
to collect arbitrary counterexamples.

## Choose a probe

Depending on the statement, try one or two of these small probes:

- instantiate zero, an origin, an empty or trivial object, or a singleton;
- test an endpoint, boundary, corner, piecewise-smooth point, or empty domain;
- remove one assumption and see whether the conclusion or proof really changes;
- instantiate the weakest stated regularity or topology;
- inspect strict versus non-strict inequalities and conclusion polarity;
- test whether a totalized operation makes a predicate vacuous outside its
  intended domain;
- delete a purportedly load-bearing field, `have`, or dependency and recheck;
- seek a minimal consumer that distinguishes two allegedly equivalent forms.

Use a concrete Lean example, a type-level reduction, or a short mathematical
counterexample. Do not infer a defect merely because an unusual case is not
interesting to the source.

## Classify the result

Distinguish a false theorem from a valid conditional theorem, an encoding that
has the wrong domain, and a proof that simply lacks an available lemma. A
counterexample to a convenient proxy does not refute the source statement; a
counterexample outside the stated hypotheses is not evidence.

When a probe changes the result, record the exact instantiation, assumptions,
and output. When it does not, say which semantic risk it actually ruled out.

## Boundaries

Prefer read-only scratch files or a temporary declaration. Do not leave a
diagnostic `sorry`, axiom, or weakened statement in accepted source. Restore
temporary edits and preserve the probe in a rejected-attempt artifact if it is
useful. Pair with [[source-fidelity]] for source meaning and
[[verification-evidence]] for kernel evidence.
