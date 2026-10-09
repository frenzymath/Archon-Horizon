---
name: horizon-report
description: Finish or hand off a Horizon assignment with evidence and the required obligation and mission updates.
metadata:
  category: operations
---

# Finish The Assigned Work

Report the result, its evidence and any remaining work with its owner. Link the
commit, PR, declaration or delivered review rather than repeating logs. The
mission and assignment instructions define completion; a provider-native goal
is optional. There is no separate general report mutation endpoint.

A task to produce a PR can finish once that PR is durably published. A task to
integrate it still needs acceptance and merge. Do not silently change one into
the other. For known review or repair work, retain the existing owner where a
supported checkpoint or authorized resume is available; otherwise give one
narrow follow-up the exact findings and existing artifacts. Possible future
feedback alone is not unfinished work.

## Required Completion Records

The current API still requires these records. Final prose does not settle them.

1. Confirm delivery of the artifact you claim. Reconcile uncertain mutations
   through their existing journal entries and operation receipts.
2. Resolve each open obligation owned by this assignment truthfully. Create an
   additional obligation only for a real requirement, not routine progress.
3. Handle relevant control notices after acting on them, and settle outstanding
   publication, external-delivery and provider work required by completion.
4. If the assigned mission's acceptance criteria are fulfilled and its children
   are closed, use `complete_mission` with the current revision. Finishing an
   assignment does not automatically close its mission. In objective mode, finishing a session and accepting a mission are distinct:
   a faithful delegation preserves an integration owner. A maintainer records
   `accept_phase` with evidence; the scheduler settles deliveries before advancing.
   Legacy runs retain explicit root closure and draining.

Use `POST /api/v3/obligations/{id}/resolve`, with `expected_revision` and one
resolution. Look up `agent schema --section obligation_resolve` only when needed:

| Outcome | Status and resolution |
| --- | --- |
| Delivered | `done`, `kind: completed`, truthful note and evidence |
| Concrete follow-up owner | `handled`, `kind: delegated`, `assignment_ids` |
| Pending scheduled owner | `handled`, `kind: scheduled`, `assignment_id` |
| Owned reconsideration | `handled`, `kind: reconsider`, `assignment_id`, note and evidence |
| Requirement legitimately replaced | `superseded`, `kind: superseded`, explanation and replacement IDs when present |

Evidence uses supported object references such as an `artifact` or `review_gate`.
For a trusted milestone check, reference its report artifact and put the check
ID and exact commit in the note; `check` and `milestone_check` are not ledger
evidence kinds. A failed or cancelled assignment is not a follow-up owner.

Native children return evidence to their parent; they do not settle the parent's
ledger. Prepared reviewers follow their supplied report lifecycle: deliver the
assessment, settle their own required records, then stop. They must not wait for
their own report to become an eligible approval, which requires their termination.

A maintainer may settle historical obligations from terminal assignments using
existing evidence. Do not restart a provider merely to edit its ledger. Apply
known finite bookkeeping changes together with current revisions and stop on
an unexpected response. Legacy child maintainers cannot close siblings or ancestors. Objective maintenance decisions are authorized within the current objective root.

If real unfinished work waits on an external event, use the
[checkpoint procedure](../horizon-delegation/references/queue.md) and leave its
obligation open. Do not mark the work complete to make the execution stop.

## Close The Phase

The root maintainer verifies the phase deliverable and settles fulfilled child
missions from the leaves upward. Read the current root mission revision, then
send `POST /api/v3/commands`:

```json
{"operation":"complete_mission","target_id":"<root-mission-id>","expected_revision":4,"args":{"note":"Accepted result and evidence links"}}
```

After success, read the current run revision and send the same endpoint:

```json
{"operation":"drain_run","target_id":"<run-id>","expected_revision":5,"args":{"note":"Phase deliverable accepted; settle remaining execution and delivery"}}
```

Use actual IDs and revisions. An `open_children` response names child missions
to reconcile, not permission to cancel your own assignment. Settle your own
obligations and receipts and return. The host completes the draining run after
physical execution and external delivery settle; do not wait for your own run
completion. In preprocessing the ready baseline packet is the phase result;
human approval and launching formalization are separate actions.

Goal ledger descriptions and comments use Markdown. Use `comment_obligation` for
an explanation or evidence update without changing disposition. `blocked` work
stays open; `delegated` names durable owners and does not claim a proof. Supersession
needs its replacement and reason. Removing required mission criteria needs a
maintainer decision, even when editing the local plan would be convenient.
