---
name: horizon-review
description: Review a Horizon PR, request a concrete repair, or accept the checked current head using its configured independent reviewers.
metadata:
  category: review
---

# Review, Repair, Accept

The maintainer owns the acceptance decision for its assigned PR or coherent
batch. Start with the exact diff, destination policy, existing findings and
available checks. Decide the next missing question; do not restart the whole
review on each round. An empty review queue is a reason to finish or wait for
real work, not to invent a library audit.

## Follow The Phase

- Preprocessing reviews the roadmap's statements, concrete definitions and
  dependency argument strictly. Completed theorem proofs are not required.
- Formalization leaves proof development in the workspace. Review changes to
  the graph's statements, dependencies and completion claims. A correction to
  an accepted public contract needs the configured strict review and adoption.
- Postprocessing reviews coherent library contributions, normally including
  their existing proofs. A separate admitted statement prototype is useful
  only when an unresolved interface choice warrants it. Judge public meaning
  and reusable definitions before proof polish; keep independent PRs moving.

Use the actual policy and `review_readiness` to determine required coverage.
For milestone contracts the current gate requires statement-fidelity,
definitions, decomposition and library-api approvals, a trusted check and an
accepted route. Postprocessing requires every enabled policy dimension. These
are enforced requirements; calling review proportional does not waive them.
See the [phase workflow](../../operations/horizon-pipeline/references/phases.md)
and [milestone protocol](../../operations/horizon-graph/references/milestones.md)
only for the policy relevant to this PR.

## Dispatch Only Missing Work

Use `review_readiness.next_actions`: its `request_template`, required fields
and `schema_command` identify the actual prepare or retry operation. Collect or
reconcile an existing owner before creating another invocation. Supply the
exact head, bounded question, relevant files and prior findings, without a
desired verdict or a copied maintainer transcript.

The configured descriptor chooses the execution mode:

| Mode | Maintainer action |
| --- | --- |
| `assignment` | Prepare at `POST /api/v3/reviewer-assignments`; the returned assignment is already queued. Delegate its returned parent obligation. |
| `subrequest` | Prepare the native invocation, spawn with its complete returned prompt, attach the actual child ID and collect it within this live execution. |

For durable reviews, keep one integration owner. An existing owner may checkpoint
on the selected reviewers' terminal conditions and release its slot; otherwise
arrange one narrower integration assignment. Reuse an already queued owner.
Include failed/cancelled outcomes, which require inspection rather than approval.
Native review requires the parent to stay active until its children have
reported and stopped. Do not switch an in-flight review's mode to evade capacity
or ownership conflicts. See [Forge protocol](references/forge-reviews.md) for
the selected mode's API details.

## Repair The Specific Findings

Read the accumulated findings and author replies. Give requested changes to the
existing active or checkpointed author, with the exact findings and affected
scope. If it has finished and cannot be resumed, arrange one narrow repair
assignment carrying the existing PR and evidence. A maintainer may make a small
authorized repair directly.

Have the affected specialists inspect the new delta. Preserve unaffected
coverage through the supported same-PR carry-forward contract when eligible;
an old approval by itself is not approval of a new head. Reuse trusted checks
only for their actual source and scope. Do not order another complete review
wave merely because the author changed a receipt or explanation.

Each specialist must resolve its own earlier blocking findings through the
prepared report's typed `assessment.resolutions`, using exact finding IDs.
A maintainer cannot copy those resolutions into its own decision or waive
independent coverage. Disagreement needs the decisive evidence, a bounded
experiment or an explicit unresolved decision, not repeated reviews until a
preferred verdict appears.

If readiness identifies an incomplete or unusable report, use its explicit
correction/retry action with the existing owner ID and a concrete repair note.
This preserves the reviewer, pins and plan. A valid approval or substantive
changes request is not eligible for a retry merely to change its verdict.

## Accept And Finish

Check current-head readiness, unresolved findings and required checks. Publish
the maintainer decision and verify its `gate_result`, then merge through the
supported Forge operation. A delivered review is not necessarily an accepted
gate. Acceptance of work intended for the default branch requires integration
there, not only merge into another proposal branch.

Return the accepted commit and evidence, or concrete outstanding findings with
their owner. Close only fulfilled missions within your authority and follow
[assignment completion](../../operations/horizon-report/SKILL.md). A child
integrator does not perform root closure. If the existing integration owner
still has real work after another session's result, use a supported checkpoint
instead of creating another broad maintainer.

## Reviewer Scope

A prepared reviewer uses its packet, assigned rubric and report template. It
does not perform general project discovery or manage the queue. Report defects
with a location, consequence and feasible correction. A clean review is valid;
optional preferences are not invented blockers. In postprocessing, inexpensive
public API improvements with a demonstrated benefit need repair or a reasoned
tradeoff.

Publish the substantive assessment under the prepared reviewer identity. Use
the required typed assessment when the packet requires it, including coverage
and prior-finding resolutions. Verify delivery, settle the prepared lifecycle,
and stop; your own approval eligibility may require your completion event.
Keep follow-ups about changed evidence rather than reproducing the original
report. The [shared rubric](references/review-contract.md) supplies judgment
criteria; [review tools](references/review-tools.md) are available for a concrete
inspection or measurement. Prepared prompts already contain the shared rubric.

Use `library-audit` only for evidence of a cumulative defect across accepted
changes. Pin the suspected interaction and representative consumer; an empty
queue or routine batch is not enough reason for a new audit.
