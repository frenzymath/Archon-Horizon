---
name: lean-performance
description: Diagnose slow Lean elaboration and builds with controlled measurements, isolating import, simplifier, typeclass, unification, and proof-search costs.
metadata:
  category: lean
license: MIT
---

# Measure Lean Performance

For maintainer/reviewer tool selection, Radar/CI evidence and benchmark requests,
read [review tools](../../review/horizon-review/references/review-tools.md).
The bundled [measurement comparator](scripts/compare_measurements.py) compares
existing Radar-format JSONL artifacts without launching builds or contacting
external services. Follow the tool guide for provenance and invocation.

Start from a reproducible slow declaration or target. Record source revision,
toolchain, dependency/cache state, command, and whether wall time includes
queueing, downloads, imports, or actual elaboration. A warm build versus a cold
build is not a valid proof-optimization comparison.

Narrow to one affected module and relevant consumer. Use available LSP timings
or Lean profiling options supported by the pinned version. Keep traces scoped
and place outputs in the run scratch directory. Read
[profiling and repair](references/profiling-and-repair.md) for the workflow and
common mechanisms.

Choose a repair based on the measured hotspot: explicit types for unification,
a direct lemma for repeated search, a smaller simp set for excessive rewriting,
or an API/instance repair for repeated consumer costs. Changing a theorem's
meaning or increasing global limits does not optimize its proof.

Compare before and after using equivalent inputs and repeat measurements when
noise could explain the difference. Record a useful representative downstream
consumer; faster definition compilation can hide slower use sites. Preserve
readable automation when no performance problem is demonstrated.

Use [lean-check](../lean-check/SKILL.md) for managed checks. Remove temporary
profiling options and outputs from published source. If a narrowly scoped
heartbeat/recursion increase is justified, explain its measured need and
limitations instead of presenting it as the optimization.

Adapted from upstream `profiling-workflows.md`, `performance-optimization.md`,
and `instance-pollution.md`; [provenance](../../_sources/lean4-skills/PROVENANCE.md).
