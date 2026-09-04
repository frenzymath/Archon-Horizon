---
name: progress-integrity
description: Review whether a Horizon task is making honest, task-scoped, convergent progress rather than producing placeholders, performative activity, or repeated no-op loops.
---

# Progress integrity

Use this lens for the task itself: compare what was requested with what the
agent actually changed and what the durable records support. It is deliberately
separate from mathematical truth, source fidelity, Lean/kernel verification,
and API quality.

## Inspect

- Read the objective, write set, status history, latest report, commits, and
  actual diff at one revision.
- Match every claimed deliverable to a concrete declaration, file, check, or
  recorded blocker. Comments, regenerated metadata, and a clean exit are not
  deliverables by themselves.
- Compare repeated rounds for new task-relevant artifacts, narrowed blockers,
  changed routes, requeues, helper churn, and sessions without terminal status.
- Look for placeholders, `sorry`/`admit`, wrappers that move the target, and
  claims of `done`, `fixed`, or `complete` unsupported by the task objective.
- Distinguish a source or proof blocker from a bad working directory, resource
  failure, staging problem, or missing artifact; route those details to the
  specialist lane.

## Report

Return `converging`, `partial`, `churning`, `stuck`, or `diverged`, with the
smallest artifact-backed explanation and one next action. State the revision,
scope, unchecked specialist questions, and confidence. File an inbox issue for
an actionable integrity problem or a memory for a reusable anti-loop lesson.

Do not mark the task done, edit the reviewed source, or decide whether a
theorem is true. Use [[review-method]] for the common evidence/report shape and
dispatch `work-reviewer` for a fresh-context implementation of this lens.
