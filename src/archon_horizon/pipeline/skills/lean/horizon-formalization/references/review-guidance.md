# Reviewing Formalization

The destination policy, intended mathematics, pinned rubric and exact reviewed
commit define the scope. State which files and claims were inspected, concrete
blocking findings, and any limitations. Keep optional improvements separate from
correctness defects. A reviewer proposes an assessment; the maintainer owns the
merge decision. A changed head invalidates any claim that the old review checked
the new code.

## Choose Proportionate Depth

Preprocessing reviews the roadmap's intended statements, definitions, dependency
graph and milestone decomposition carefully. A skeleton may intentionally contain
admitted proofs; make their ownership visible. Do not demand completed proofs as
a condition for accepting a sound plan.

During formalization, review roadmap changes for mathematical correctness and
coherent dependencies. Repair straightforward defects in the PR when authorized,
then inspect the resulting head. The workspace remains a place for rapid proof
development; do not apply library polish as a per-commit gate.

Postprocessing reviews the destination library strictly. Work from its principal
theorems and definitions toward supporting infrastructure and proofs. Establish
the intended public API early through concrete code and discussions, and revise
it when evidence warrants. A separately approved roadmap is not a prerequisite.
Normally review a coherent family adapted with its existing proofs. A separate
statement prototype is useful only when a concrete unresolved interface choice
warrants it. Such a prototype may use explicit `sorry` with visible proof status
and a durable owner; accepting its statements is not proof completion. Assess
supporting interfaces for their actual use instead of importing a source
workspace wholesale or forcing every port through a statement-only PR.

## Statements and Definitions

Compare the actual Lean declaration with its precise source or stated purpose.
Inspect quantified variables, implicit parameters, typeclass assumptions, domains,
boundary cases and the definitions on which the statement depends. Look for
vacuous predicates, impossible hypotheses, circular certificates, and assumptions
that merely restate the result. Building successfully does not establish this
alignment. Independent informal translation can clarify a disputed statement.

Definitions deserve early scrutiny: use established mathematical notions and
existing abstractions where they fit. Test whether downstream users can apply
them naturally. Consider inexpensive generalizations with an actual use or a
clear simplification; avoid speculative layers. A book formalization preserves
source alignment and explains intentional differences rather than silently
changing its mathematics.

## Library API and Proofs

Inspect nearby accepted modules and relevant review discussions in the target
repository. Names should describe reusable mathematics, with appropriate
namespaces and hypotheses. Remove unnecessary nesting, duplicated infrastructure,
isolated helpers and project-specific names when a conventional API is available.
An unmerged or rejected upstream PR is context to evaluate, not a precedent by
itself. Record the commit or discussion used for an analogy.

Once statements are settled, inspect proof dependencies and admissions. Search
for existing lemmas and more direct arguments before retaining elaborate local
infrastructure. Prefer concise, readable proofs over character-count contests.
Use the repository's checks and any applicable axiom/statement comparison tools;
record their scope and results. A tool result supplements mathematical judgment.

## Performance and Repository Quality

Measure relevant elaboration/build costs against the same dependency versions
and comparable cache conditions. Investigate heavy imports, expensive instance
search, simplifier configuration and repeated infrastructure. Show a measured
improvement before trading away clarity for speed.

Check that the repository has an intentional module layout, supported toolchain,
reproducible dependency pins, useful CI and suitable metadata for its audience.
Use recognized projects with similar aims as comparisons. Propose toolchain
updates with compatibility and build evidence, not simply because a newer
version exists. Remove scratch notes or redundant files from the destination
library only within the authorized change; preserve useful work in its workspace.

When a blueprint exists, reconcile its theorem links, explanations and references
with the accepted code. Use catalogued references with precise source locations
and a reproducible BibTeX export suited to the repository. A project without a
blueprint need not acquire one just to satisfy a rubric.

## Community Sources

Consult the current destination instructions first. These official mathlib
resources provide comparisons; they do not override a different project's
documented conventions or its pinned Lean version:

- [Contribution guidance](https://leanprover-community.github.io/contribute/index.html)
- [Naming conventions](https://leanprover-community.github.io/contribute/naming.html)
- [Library style guidelines](https://leanprover-community.github.io/contribute/style.html)

The style guide covers organization and documentation as well as formatting.
Treat conventions as guidance requiring context, rather than automatically
applying every current syntax example to an older pinned toolchain.
