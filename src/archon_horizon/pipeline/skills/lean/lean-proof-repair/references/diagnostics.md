# Diagnostic Routing

Read only the branch matching the observed failure.

| Diagnostic | Inspect First | Useful Next Change |
| --- | --- | --- |
| Unknown identifier | Exact declaration spelling, namespace, defining file, installed version | Search the pinned source; qualify the name or add its actual import |
| Application type mismatch | Candidate's full type, goal type, implicit arguments, coercions | Supply a specific parameter or a proved conversion; avoid guessing casts |
| Failed typeclass synthesis | Class requested, available local instances, expected carrier/structure | Find the existing instance or make the intended structure explicit |
| Rewrite did not match | The actual elaborated expression and rewrite orientation | Use a relevant equality, `change` when definitionally equal, or a focused `conv` |
| Unsolved goal after simplification | Remaining proposition and missing hypotheses | Supply the mathematical fact rather than expanding the global simp set |
| Timeout or heartbeats | Slow declaration/tactic, resource contention, repeatability | Profile and narrow the expensive step before changing limits |
| Works locally, fails downstream | Imports, compiled artifact revisions, public visibility, instances | Rebuild changed dependencies and check from a minimal real consumer |

For instance failures, changing the order of declarations or adding a second
ambient structure may silently change inferred terms. Inspect the elaborated
type and make the intended parameters explicit. Introduce a local instance only
when its mathematical justification and scope are clear; a global instance is
a public API change with possible ambiguity and performance costs.

For timeouts, distinguish waiting for a build slot from slow elaboration. A
managed build exit indicating deferral is not a failed theorem. Do useful
independent work or arrange a later check through Horizon instead of bypassing
the build queue. If actual compilation fails, compare the same target and
toolchain with comparable cache conditions.

If a proof is slow, locate the expensive tactic or unification problem using
available profiling tools or narrowly scoped traces supported by that Lean
version. First try explicit types, smaller intermediate facts, narrower tactic
inputs, and reusing existing lemmas. Splitting a large term can make inference
tractable without changing the theorem. Broad tracing creates large logs, so
confine it to the failing declaration and remove it afterward.

Changing reducibility, introducing opaque wrappers, or raising resource limits
can sometimes be justified, but each changes costs or behavior for consumers.
Measure the result and check downstream use. Do not specialize a public theorem
to a concrete type merely to get a green build; preserve its intended scope or
have the mathematical change reviewed explicitly.
