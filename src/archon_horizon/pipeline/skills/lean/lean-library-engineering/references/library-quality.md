# Library Quality

## Destination Evidence

Read the destination's contributor guide, toolchain, Lake configuration, CI,
linters, and adjacent accepted modules. For mathlib-style work, consult the
official [style guide](https://leanprover-community.github.io/contribute/style.html)
and [naming guide](https://leanprover-community.github.io/contribute/naming.html),
then verify applicability to the pinned revision. Newer module syntax and
visibility rules must not be imposed on an older project blindly.

A recent accepted PR can illustrate a convention; an open or rejected PR is
discussion evidence, not an accepted standard. Follow review reasoning and final
code rather than copying a superficially similar patch. Do not contact public
communities or publish outside the assignment's configured integrations without
authorization.

## Definitions And Statements

- Compare hypotheses, quantifiers, nonemptiness assumptions, universes, and
  degenerate cases with the source. For book formalization, record precise
  source locations and explain a faithful representational difference.
- Inspect definitions before accepting an easy proof. An assumed conclusion
  hidden in a structure field, instance, or callback remains an assumption.
- Prefer established notions and their APIs when mathematically appropriate.
  A new wrapper needs a concrete benefit such as an invariant or useful bundle;
  nested records and projections alone do not create reuse.
- Generalize when removing an unused restriction or using an existing weaker
  class is cheap and clarifies the result. Check that the generalization is
  mathematically correct and still usable. Do not broaden the assignment into
  a new abstraction project simply because further generality is possible.
- Use subject-based names and namespaces. Avoid mission numbers, theorem
  numbers, temporary names, or application-specific prefixes for general facts.
  Source theorem numbers belong in documentation and citations.

## Proofs And Performance

Try a direct existing lemma, an equivalent standard characterization, or a
shorter mathematical argument before locally golfing a long tactic script.
Factor repeated mathematics into shared lemmas with necessary hypotheses only.
Keep proof-specific machinery private or local when exposing it does not help
consumers. Do not split a readable argument according to fixed line thresholds.

Check simp lemmas for orientation and their effect on intended normal forms.
Check new instances for ambiguity, synthesis loops, and scope. Prefer a small
consumer example over asserting that an API is reusable from its signature.
Keep explicit proof-search suggestions when they make the proof more stable;
do not mechanically replace every use of `simp`, `aesop`, or other automation.

Measure slow compilation using the same source revision, target, toolchain,
machine, and comparable cache state. Separate elapsed time from deterministic
heartbeats and distinguish dependency rebuild cost from theorem elaboration.
Record a before/after result if claiming an optimization. Constrain imports,
search premises, or intermediate terms where measurements identify a problem.
Do not add opaque wrappers or permanently enlarge global limits as a default
optimization strategy.

## Repository And Documentation

Keep a coherent module tree and a supported root import path. Use destination
automation for generated import files where present; do not assume every Lean
repository has mathlib's generators. Remove scratch artifacts from the library
change, keep real tests in the project's test locations, and consolidate shared
infrastructure instead of copying it beside each theorem.

For a standalone library, check that the documented build command, toolchain,
dependency pinning, license/attribution, and CI match the actual repository.
Recommend optional ecosystem tools only when they serve the project's stated
purpose. A comparator, blueprint, documentation generator, or toolchain upgrade
is not a universal publication requirement.

Write enduring module/declaration documentation about the result and its API.
Put migration history in the PR. If the project has a blueprint, align its
statements, dependency links, and references with the accepted Lean revision.
Use the project's reference catalog and consistent citation keys/BibTeX; do not
create redundant bibliographies for each node. Check generated documentation or
blueprint outputs when they are affected and their tooling is available.
