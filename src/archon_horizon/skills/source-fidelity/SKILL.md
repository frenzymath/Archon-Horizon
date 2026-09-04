---
name: source-fidelity
description: Compare a source-backed mathematical claim with its blueprint and Lean declaration, preserving objects, hypotheses, domains, quantifiers, regularity, and conclusion rather than accepting a convenient equivalent.
---

# Source fidelity

Use this lens whenever a definition, theorem, or public API is anchored to a
book, paper, lecture note, or other source. Read the actual source before
trusting a citation. A compiling declaration can still formalize a different
claim.

## Compare the contract

Put the source sentence, blueprint statement, and Lean type side by side. Check:

- the same objects, maps, structures, and ambient category;
- domain, codomain, quantifiers, order, endpoints, and finiteness conditions;
- regularity and locality assumptions, including smoothness conventions;
- topology, dimension, connectedness, completeness, and algebraic hypotheses;
- conclusion polarity, strict versus non-strict inequalities, and exceptional cases;
- intrinsic statements versus chart, coordinate, finite, or computational models.

Treat a proxy as an implementation aid, not as the source theorem. Name it as a
model or auxiliary result and expose an equality, equivalence, or implication
bridge before presenting it as the same concept.

## Distinguish legitimate variants

An explicit stronger-hypothesis corollary, a conditional interface, and a
faithful source theorem are different declarations. Do not silently move
proof-route assumptions (for example completeness or global regularity) into
the source-facing statement. If a source is ambiguous or internally omits a
needed bound, record the interpretation and keep the original wording visible.

Search for an existing formalization before adding a second definition. If two
objects have one mathematical name, require a named bridge with its exact
hypotheses, or say clearly that they are intentionally distinct.

## Evidence and report

Record the source file/page actually read, the declaration type and location,
and any bridge or missing consumer. A source anchor proves provenance, not
semantic equality. Report `confirmed`, `partial`, `unverified`, or `no issue`
with the reason. File a focused issue for a mismatch; do not repair a frozen
source or blueprint while acting as an independent reviewer.

For edge cases, pair this lens with [[semantic-adversarial]]. For `\source`,
`\lean`, and `\leanok` attachment checks, use [[blueprint-integrity]].
