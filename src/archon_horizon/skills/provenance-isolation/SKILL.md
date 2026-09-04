---
name: provenance-isolation
description: >-
  Review workspace and commit integrity: write-set boundaries, project
  ownership, ledger provenance, generated files, scratch artifacts, and
  concurrent-run interference.
---

# Provenance and isolation

Use this lens before accepting a checkpoint in a multi-project or concurrent
workspace. Read [[project-git]] and preserve the distinction between project
source, shared references, generated graph state, and Horizon control files.

## Audit the boundary

Record the pre-review revision and worktree state, then inspect:

- every changed, staged, and untracked path against the task's project/write set;
- cross-project edits, frozen files, references, generated graph/cache files,
  scratch drafts, temporary outputs, and stale locks;
- commit author and Archon run/session/role/task/projects trailers;
- whether a commit or integration staged unrelated control state despite an
  explicit path list;
- concurrent live runs, shared build/index locks, and files changing during the
  review;
- whether an interrupted command left detached `lake`/`lean` children or
  half-written artifacts.

Treat a path mismatch as a provenance issue even when the mathematics is sound.
Do not rewrite history, delete another team's work, or reseed a shared index
while live runs may depend on it. Preserve unrelated edits and identify the
owner or coordination thread for corrective work.

## Evidence and handoff

Give exact paths, commit ids, trailers, process/lock evidence, and the safe
boundary for repair. Classify source damage, generated churn, attribution loss,
and expected concurrent state separately. Pair with [[graph-traceability]] and
[[run-health]]; use [[review-method]] to state snapshot and non-goals.
