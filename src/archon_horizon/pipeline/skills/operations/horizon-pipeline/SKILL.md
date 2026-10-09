---
name: horizon-pipeline
description: Follow the worker or maintainer workflow inside a dispatched Horizon assignment. Covers phase outcomes, ownership, delivery and when to request an operational audit.
metadata:
  category: operations
---

# Horizon Assignment

You are working within **Archon Horizon**, a control plane for Lean formalization
projects. Its source is at
[frenzymath/Archon-Horizon](https://github.com/frenzymath/Archon-Horizon).
If a local source checkout matching the installed version is available, consult
the relevant code when CLI help and API schemas do not explain observed behavior.
Your project workspace is separate from Horizon's source; `$PATH` locates tools
and does not necessarily contain a source checkout.

Your assignment states the result to deliver. Its mission supplies the objective,
scope and acceptance criteria. Work within that scope; the run selects the phase
and repositories. The host handles execution leases, retained context and queue
admission. The effective goal is the mission, acceptance criteria and open Markdown ledger. A native harness goal mirrors this contract.

Read this entrypoint once. Use the supplied task and current evidence to begin.
Run `horizon-pipeline agent context` when you need omitted records, current
obligations or notices. Continue retained work after a checkpoint; a continuation
is not a new planning exercise. Prepared reviewers follow their supplied packet
and lifecycle directly, without this general startup.

## The Two Roles

**Worker:** produce the assigned result, validate the relevant scope, publish it,
and return evidence. For a review correction, repair the identified defect in the
existing PR and preserve applicable earlier work. Delivery need not wait for merge.

**Maintainer:** choose ready work, delegate when useful, review delivered results,
request precise repairs, and accept or reject according to repository policy.
Retain responsibility for integration. In objective mode, a bounded maintenance session can accept the phase or repair ownership within that objective. Legacy runs retain their root-maintainer contract.

Planning is an activity within these roles. Use a helper for an independent
question; do not add a planning session before a task whose inputs are clear.
An unused slot alone is not a reason to create work.

## The Three Phases

| Phase | Workers produce | Maintainers accept |
| --- | --- | --- |
| Preprocessing | Literature, concise objective and a proposed roadmap strategy, with Lean skeletons where useful | Roadmap PRs and the phase strategy with proportionate source evidence |
| Formalization | Proofs in the shared workspace and source-bound graph progress | Roadmap graph and contract changes; workspace work has no routine PR gate |
| Postprocessing | Reused, adapted or remade source results in the destination library | Library PRs with sound public interfaces and proportionate verification |

Objective runs use one concise phase skill:
[horizon-preprocessing](../horizon-preprocessing/SKILL.md),
[horizon-main-work](../horizon-main-work/SKILL.md), or
[horizon-postprocessing](../horizon-postprocessing/SKILL.md).
The same queues and session lifecycle serve every phase. Maintainer acceptance
advances requested phases by default; `auto_advance: false` pauses for human approval.
[Legacy phase details](references/phases.md) apply to explicitly legacy runs. A Lean build checks elaboration, not
whether the statements mean the intended mathematics.

## Work, Review, Repair

1. Check the relevant graph nodes, source revision and existing owner. Narrow
   tasks need the relevant records, not a review of the whole run.
2. Do the work, or delegate independent parts with concrete inputs, acceptance
   criteria and an integration owner. Missions describe responsibility; graph
   edges describe mathematical prerequisites. A narrower mission does not prove
   that its interfaces compose correctly.
3. Publish the result and its checks. Maintainers read previous findings, choose specialists when useful and honor
   any configured required coverage, and request specific corrections on the existing PR.
   Reviewers use the descriptor's configured native or durable invocation mode.
4. Account for delivered work and remaining commitments using
   [horizon-report](../horizon-report/SKILL.md). When awaiting a known external
   result, checkpoint with an event condition that also handles failure. This
   releases execution capacity. Reuse an active or checkpointed owner for a
   fitting repair; see horizon-delegation for completed-owner recovery.

Native children are collected by their parent. Queued assignments own durable
work independently. A child can close its own subtree, never siblings or ancestors.
Objective maintainers use `accept_phase` with source-bound evidence; legacy root
maintainers close the phase and drain the run. Reuse accepted evidence;
it does not commission another review without an unresolved acceptance question.

## Tools When Needed

Paths below are relative to this skill. The full skill and descriptor indexes are
`$HORIZON_SKILLS_DIR/SKILLS.md` and `$HORIZON_SKILLS_DIR/SUBAGENTS.md`.

| Action | Procedure |
| --- | --- |
| Create narrower work, adjust the queue, or wait on a result | [horizon-delegation](../horizon-delegation/SKILL.md) |
| Publish or recover workspace changes | [horizon-workspace](../horizon-workspace/SKILL.md) |
| Propose or amend a destination PR | [horizon-forge](../horizon-forge/SKILL.md) |
| Review a PR and handle its findings | [horizon-review](../../review/horizon-review/SKILL.md) |
| Update graph nodes or milestone contracts | [horizon-graph](../horizon-graph/SKILL.md) |
| Develop or check Lean | [horizon-formalization](../../lean/horizon-formalization/SKILL.md) |
| Settle completion, receipts or a transport failure | [horizon-report](../horizon-report/SKILL.md) |
| Discuss a concrete decision | [horizon-zulip](../horizon-zulip/SKILL.md) |
| Diagnose unexpected coordination or resource behavior | [horizon-operations](../horizon-operations/SKILL.md) |

Use `horizon-pipeline agent request METHOD PATH 'JSON'` for API mutations; it
journals the request and supplies the idempotency key. Large bodies can use
`--body-file "$TMPDIR/request.json"`. Discover an unknown operation with a
scoped `agent schema --section ... --name ...` query. There is no requirement
to enumerate schemas before work. After an uncertain response, inspect
`agent pending` and reconcile the same intent before issuing a replacement.

Use only execution-provided credentials. Preserve host-managed caches and live
workspaces. Put scratch work under the inherited disk-backed `$TMPDIR`.
Check relevant notifications at useful boundaries and settle actionable control
notices. Zulip carries coordination, human discussions, nontrivial brainstorms and failed
routes another owner should know. Link durable versioned notes for detailed
knowledge; avoid session diaries and repeated progress reports.

## Objective Queues And Recovery

Work and Maintenance have independent queue bounds, slots, model defaults and
budgets, sharing physical host/provider capacity. One bounded planner already has
one scheduler-owned successor. Do not enqueue your current mission, a renamed
copy of it, or another recurring planner. Queue distinct independently useful
missions, using conditions only for genuine dependencies.

An eligible `awaiting-review` PR or issue receives one maintenance owner per
review round. Use `request_review` with new evidence after a repair and
`settle_review` for the assigned generation. Duplicate observations reuse the
same owner; a late label delivery cannot erase a newer durable request.
Use `request_maintenance` on the run for phase acceptance or another bounded
privileged decision, with a note and evidence. This grants no extra permission.

Interruptions preserve the same session and native context. The scheduler resumes
it with bounded backoff and persistent attempt history. Paused recovery requires
`resume_session` with a diagnosis; do not queue a replacement or toggle labels to
reset failure budgets. Shared provider circuits need an operator `reset_circuit`
after repairing the underlying account. Unknown physical stops retain capacity.
Inspect admission and resource diagnostics before dispatching more work.

## When Something Looks Wrong

For unexplained idle work, repeated failed handoffs, conflicting owners or repeated
API failures, inspect `horizon-pipeline agent context --view operations`.
It links current owners, activity, admission reasons, reviews and host health.
A known dependency wait with a live producer is normally healthy.

For a diagnosis requiring independent investigation, launch the native
`subagents/validation/orchestration-auditor.md` helper with the symptom and
affected IDs. It reports evidence and a proposed repair to you. Apply authorized
repairs through normal APIs or identify the owner needed for an operator action.
Do not turn diagnosis into a standing session or require every worker to monitor
global activity.
