---
name: release-reproducibility
description: Calibrate a public or milestone formalization claim against a pinned revision, reproducible commands, shipped artifacts, dependency closure, and documented residual boundaries.
---

# Release reproducibility

Use this lens for a release, public progress announcement, milestone handoff,
or claim that a formalization package is complete. It composes evidence from
other lanes; it is not a replacement for mathematical review.

## Check the package

- Pin the source revision, toolchain, dependency lock, project manifest, and
  generated graph/reference snapshot used by the claim.
- Re-run the documented focused and headline commands from a clean or clearly
  described checkout; record working directory, target closure, diagnostics,
  warnings, artifacts, and exit status.
- Confirm that shipped modules, blueprint anchors, generated files, and
  downstream consumers are reachable and reproducible, rather than present
  only in an untracked or cached workspace.
- Keep mathematical, kernel/axiom, graph, documentation, and release verdicts
  separate; list conditional producers, open obligations, and human review.

Pair with [[verification-evidence]], [[graph-traceability]],
[[provenance-isolation]], and [[review-method]]. Do not infer release readiness
from a green root build or a rendered dashboard alone.

## Report

State revision/toolchain, commands, artifact manifest, closure and environment,
per-layer status (`reproducible`, `partial`, `unverified`, or `failed`), residual
boundaries, unchecked scope, and confidence. Do not edit release metadata or
mark completion.
