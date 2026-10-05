# Profiling And Repair

## Isolate the cost

Separate scheduler/build-slot wait, dependency fetch, import loading, elaboration,
kernel checking, and native compilation. Narrow the target using the project's
managed build helper, preserving its admission policy. A timer around a queued
build measures end-to-end latency, not just Lean performance.

Lean versions supporting profiler traces can use temporary local options such
as `set_option trace.profiler true` and `trace.profiler.threshold`. Check option
availability and units against the pinned version before relying on them.
Some versions support heartbeat-based profiling and Firefox Profiler JSON
output. Use the run's actual scratch path for output, not a shared `/tmp` name.
Do not enable verbose traces across the whole library by default.

For a tactic hotspot, inspect goal/context immediately before it, reduce the
example, and trace only the suspected subsystem. A scratch placeholder can
help distinguish signature elaboration from proof-body cost, but that experiment
is not proof evidence and must never replace the submitted theorem.

## Match the mechanism

| Observation | Investigation | Candidate repair |
| --- | --- | --- |
| Deep application elaborates slowly | Intermediate inferred types and metavariables | Typed `have` steps or explicit arguments |
| `simp` rewrites far beyond the goal | `simp?`, reduced lemma set, scoped trace | Stable local normalization or missing direct lemma |
| Instance synthesis loops or explodes | Selected instances and competing structures | Localize/repair instances, reuse canonical representation |
| Every consumer needs large transports | Definitional equality and wrapper layers | Bridge lemma or representation repair |
| Large umbrella imports dominate | Actual declarations needed and module graph | Import a lower appropriate module |
| Repeated automation rediscovers one fact | Existing theorem search and proof terms | Reuse or extract the mathematical fact |

Do not treat this as a ranking of tactics by speed. `simp`, `omega`, `grind`,
and other solvers can be excellent and fast for their intended domain. Measure
the actual context. A short direct proof can also trigger costly implicit
unification; explicit intermediate types can improve it without changing the
mathematics.

## Resource limits and evidence

First distinguish a legitimate expensive proof from an instance loop or bad
normal form. Increase limits only narrowly when there is a reason and evidence
the computation terminates usefully. Avoid global `maxHeartbeats`/`maxRecDepth`
overrides, disabling linters, or hiding warnings to make a benchmark green.

Record a before/after table containing identical target, version, cache state,
sample count, wall time or heartbeats, and relevant caveats. If timing varies
widely, report a range instead of a precise multiplier. Recheck consumers and
axioms when changes involve proof strategy or trusted computation.

For parallel experiments, use separate working trees or disjoint owned files;
shared mutable source makes timing and correctness evidence ambiguous. Remove
failed experiments and generated logs from the publication diff.
