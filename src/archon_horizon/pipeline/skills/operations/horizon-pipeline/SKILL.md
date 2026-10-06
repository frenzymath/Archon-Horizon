---
name: horizon-pipeline
description: Follow the worker or maintainer workflow inside a dispatched Horizon assignment. Covers phase outcomes, ownership, delivery and when to request an operational audit.
metadata:
  category: operations
---

# Horizon Assignment

Your assignment states the result to deliver. Its mission supplies the objective,
scope and acceptance criteria. Work within that scope; the run selects the phase
and repositories. The host handles execution leases, retained context and queue
admission. A harness goal is optional and does not create another task.

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
Retain responsibility for integration. The root maintainer also records overall
phase completion. A child maintainer owns only its mission subtree.

Planning is an activity within these roles. Use a helper for an independent
question; do not add a planning session before a task whose inputs are clear.
An unused slot alone is not a reason to create work.

## The Three Phases

| Phase | Workers produce | Maintainers accept |
| --- | --- | --- |
| Preprocessing | Roadmap milestones and dependencies, Lean statements and all supporting definitions | Roadmap PRs with faithful contracts and concrete definitions; a ready baseline packet ends preprocessing |
| Formalization | Proofs in the shared workspace and source-bound graph progress | Roadmap graph and contract changes; workspace work has no routine PR gate |
| Postprocessing | Reused, adapted or remade source results in the destination library | Library PRs with sound public interfaces and proportionate verification |

Read the relevant section of [phase details](references/phases.md) when selecting
work or deciding acceptance. Human approval of a baseline and launching proof work
are separate from finishing preprocessing. A Lean build checks elaboration, not
whether the statements mean the intended mathematics.

## Work, Review, Repair

1. Check the relevant graph nodes, source revision and existing owner. Narrow
   tasks need the relevant records, not a review of the whole run.
2. Do the work, or delegate independent parts with concrete inputs, acceptance
   criteria and an integration owner. Missions describe responsibility; graph
   edges describe mathematical prerequisites. A narrower mission does not prove
   that its interfaces compose correctly.
3. Publish the result and its checks. Maintainers read previous findings, select
   policy-required reviewers, and request specific corrections on the existing PR.
   Reviewers use the descriptor's configured native or durable invocation mode.
4. Account for delivered work and remaining commitments using
   [horizon-report](../horizon-report/SKILL.md). When awaiting a known external
   result, checkpoint with an event condition that also handles failure. This
   releases execution capacity. Reuse an active or checkpointed owner for a
   fitting repair; see horizon-delegation for completed-owner recovery.

Native children are collected by their parent. Queued assignments own durable
work independently. A child can close its own subtree, never siblings or ancestors.
The root maintainer reuses accepted evidence to close the phase and drain the run;
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
notices. Zulip is for decisions and help; it is not another progress ledger.

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
