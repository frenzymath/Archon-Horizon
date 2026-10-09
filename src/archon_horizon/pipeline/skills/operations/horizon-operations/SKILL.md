---
name: horizon-operations
description: Diagnose a concrete Horizon ownership, queue, handoff, API or resource anomaly using the shared operational view. Use on demand when behavior is unexpected, not for routine work or mathematical review.
metadata:
  category: operations
---

# Diagnose An Operational Anomaly

Start with the observed symptom, affected run or assignment, and the result that
was expected. Read `horizon-pipeline agent context --view operations`. This is a
bounded view of owners, last activity, queue blockers, capacity, review readiness,
verification jobs and sanitized host health. Follow its links only where needed
to answer the question. Missing rows in a truncated collection are not evidence
of missing work; use the collection drilldown or report the visibility limit.

## Follow The Blocked Result

Identify the result needed next, its owner, and the event that permits progress.
For a queued owner, inspect `why_waiting` and its start condition. For a running
owner, compare recent activity with changed artifacts or review evidence. A busy
session can be stuck, and a quiet queue can be correctly waiting on a build.
For a finished owner, inspect its actual receipt and remaining commitment.

Use the snapshot's assignment and activity links for focused investigation.
PR `review_readiness` supplies exact-head blockers and next actions. Job status
and check receipts have different IDs: a queued verification is not a passed
check. Shared capacity counts are observations, not reservations.

Common distinctions:

| Evidence | Interpretation and next step |
| --- | --- |
| Known dependency with a live producer | Keep the owner and event wait; no replacement task is needed |
| Unowned required result, or failed producer | Give the maintainer the missing result and retained evidence; reuse or repair ownership |
| Repeated requests with no changed artifact or decision | Locate the rejected operation or disputed finding before trying again |
| Successful review but merge blocked | Inspect current-head coverage, delivery and unresolved findings; do not seek an extra approval blindly |
| Workspace or host admission blocked | Report the specific resource or preparation reason and responsible owner |
| Transport outcome unknown | Reconcile the same journaled intent using its operation receipt |

## Use A Native Audit Helper When Useful

Read `$HORIZON_SKILLS_DIR/subagents/validation/orchestration-auditor.md` and pass
its body to a native child along with the symptom, IDs, expected result and any
already observed evidence. Ask a bounded question. Continue independent work
while it investigates, then collect its result.
If native helpers are unavailable, perform this bounded investigation yourself
using the same view; do not create a recurring audit assignment as a fallback.

The audit is an investigation instruction, not a permission role or a separate
security boundary. The child inherits the parent's environment and authority.
Instruct it to make observations and recommendations only. The parent owns any
queue changes, notices or repair delegation.

## Return A Decision

Report the observed evidence and time, expected versus actual behavior, diagnosis
with uncertainty, smallest justified repair, responsible owner, and the event or
receipt that would show recovery. Include a no-action result when the wait is
healthy. A completed audit or posted message does not establish recovery.

Use authorized typed operations for a repair. Do not weaken review gates, delete
registered workspaces, obtain host credentials or infer permission to clean disk.
For an operator-only action, leave a precise incident with affected resources
and preserved evidence. Report a material incident through the run's configured
operations discussion using horizon-zulip; avoid repeating unchanged reports.

Stop after answering the question. Reinvestigate when relevant evidence changes
or a stated recovery condition fails, not on a periodic timer.

## Resource And Recovery Decisions

Inspect host resource observations and admission reasons before dispatching a
replacement for failed work. Agent slots, native child reservations, provider
limits and compiler slots are distinct. Memory/cgroup and CPU/I/O pressure pause
new admissions; active work keeps its context. Unknown measurements remain unknown.
A repeated provider outage opens a shared circuit; an operator repairs the cause
and records `reset_circuit`. Session `resume_session` decisions keep attempt history.

Managed caches prune only rebuildable leased outputs. For a settled generated
checkout, a maintainer may request `retire_workspace` with a reason. The server
refuses live or suspended owners, unresolved commitments, pinned inputs and
unpublished work. The host verifies clean Git state and publication of every
local commit before removing one retired checkout per five-minute cleanup pass.
Inspect `cleanup_error` when deletion was refused. Preserve all source, native
contexts and unmanaged directories that have no such verification.
