# Trust And Closure

## Name the claim being certified

A checked skeleton establishes that declarations elaborate, not that their
proofs are finished. An admission-free consumer of an admitted prerequisite
is a useful conditional result. Keep the prerequisite, owner, and exact
dependency visible; do not call the whole development unconditional.
Do not hide the consumer's original hole in a new nested admitted helper and
present that as progress in proof closure.

Check source correspondence and trust separately. A theorem of the form
`(h : Conclusion) : Conclusion` can have a normal axiom report; the difficult
construction is still assumed. A proof producing a certificate is substantive
only when the producer follows from the original hypotheses and its consumer
reaches the intended conclusion.

## Axiom inspection

In a scratch module importing the exact checked project module, inspect:

```lean
import MyProject.Result
#print MyProject.mainResult
#print axioms MyProject.mainResult
```

Replace the names with the actual module and declaration; this is an inspection
template. Use the assignment's scratch directory and managed checking path.
If module artifacts are absent, prepare the affected imports before this
check. Never infer a pass from an unknown identifier or a failed command.

`sorryAx` signals an admitted dependency. Find whether it is directly in the
target, inside an untracked helper, or in a declared conditional prerequisite.
For a custom axiom, trace where it enters and what claim it assumes. Compare
`Classical.choice`, `propext`, `Quot.sound`, and any computation-related trust
primitive against the project policy and pinned Lean version rather than a
universal string allowlist. A zero-length axiom list alone says nothing about
source alignment or whether required outputs were published.

Source searches for `sorry`, `admit`, `axiom`, and trust-altering declarations
help locate suspects. They miss imported admissions and may match comments,
examples, or deliberately unfinished scaffolding. Report the declaration
actually checked and the scan's coverage. Anonymous examples and inaccessible
private names require inspection in their defining context; do not claim a
whole-file axiom audit when the tool only covered exported names.

## Computation and shortcuts

Distinguish kernel reduction, proof-producing tactics, and native computation
whose trust depends on implementation/version. Read the declaration's actual
axioms and project policy; do not ban fast tactics merely by name or assume
that a tactic is trusted solely because compilation succeeds.

Check whether a computational result applies to the mathematical domain: a
finite enumeration needs a coverage argument, a floating-point calculation
needs the relevant soundness bound, and a coordinate model needs its bridge.
Avoid replacing an unproved theorem with an `axiom` or adding inconsistent
hypotheses to make all goals trivial.

When eliminating an axiom or admission, preserve its intended statement,
search pinned libraries, and prove the missing construction or delegate a
precise prerequisite. If the statement is false, preserve a counterexample
and correct the mathematical contract through the project's workflow. Do not
silently weaken it and count the axiom as solved.

## Reproducible closure evidence

Record repository and revision, toolchain, dependency lock, exact command,
target, exit status, and material diagnostics. Separate a local working tree
check from a published commit check. Reuse valid evidence when unchanged inputs
are established; rerun when signatures, imports, dependencies, or build inputs
changed. For a release, check required root targets and representative
consumers, not only the edited file.

Adapted from Horizon's mathematical review guidance and selected upstream
axiom-elimination principles; [provenance](../../../_sources/lean4-skills/PROVENANCE.md).
