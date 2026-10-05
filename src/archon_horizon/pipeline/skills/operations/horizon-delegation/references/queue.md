# Queue and durable ownership

Read the mission contract and relevant current owners before adding work. A
planning or integration owner also checks compatible capacity when dispatching.
The mission tree assigns responsibility; start conditions express required
evidence. A mission move does not replace a dependency or reserve capacity.

Use actual UUIDs from the current context in payloads; readable handles are for
prose. Create a narrower mission through `POST /api/v3/records/mission` before
its assignment. Inspect the current `MissionCreate` schema; for example:

```json
{
  "project_id": "<project-id>",
  "parent_id": "<own-mission-id>",
  "expected_parent_revision": 4,
  "title": "Uniform estimate for the selected family",
  "objective": "Prove the uniform estimate at the accepted hypotheses in Math/Estimate.lean.",
  "acceptance_criteria": [
    "The cited estimate has the accepted statement and a checked proof.",
    "The handoff names the published commit, declaration and validation result."
  ],
  "delegation_note": "This supplies the bound used by the parent convergence argument. The parent owns common interfaces and integration.",
  "max_open_children": 2
}
```

Omitting `scope` inherits the parent's scope. To narrow it explicitly, use
`node_ids`, `document_ids`, and `repository_paths` within `scope`; each repository
entry has `repository_id` and a relative `path` (null means the whole repository).
Supplying a scope outside the parent's grant is rejected. Creating the child
advances the parent revision, so reread it before another delegation. The
server checks structural containment; you must still justify the narrower
mathematical outcome.

Then create its owner through `POST /api/v3/records/assignment`:

```json
{
  "run_id": "<run-id>",
  "mission_id": "<narrow-mission-id>",
  "parent_id": "<own-assignment-id>",
  "role": "worker",
  "functions": [],
  "instructions": "Prove the uniform estimate in Math/Estimate.lean at the cited source revision. Keep its hypotheses unchanged; return the published commit, exact declaration, and narrow check result. Integration remains with the parent.",
  "start_condition": {
    "version": 1,
    "expression": {"op":"publication_verified","publication_id":"<producer-publication-id>"}
  }
}
```

The mission supplies the goal; an existing appropriate child mission can be
reused. `parent_id` in the assignment payload is an assignment ID, not a mission
ID. Time bounds are optional RFC3339 timestamps with timezones;
`not_before` must precede `expires_at`. Omit unset values or use the contract's
null where accepted. Do not set deadlines that discard needed work silently.

Only catalog functions such as `planner`, `reviewer`, and `debugger` are valid;
reviewer rubric names are descriptor identities, not execution functions. Prefer
the dedicated reviewer preparation route for review work.

## Reorder without changing conditions

`POST /api/v3/commands`:

```json
{"operation":"move_before","target_id":"<pending-maintainer-id>","expected_revision":4,"args":{"other_id":"<pending-worker-id>"}}
```

Both assignments must be pending in the same run. Moving an assignment cannot
override its start condition, missing capacity, or `not_before`. Refresh after a
revision conflict; do not retry an obsolete ordering decision blindly.

Mission movement is a different operation: `PATCH /api/v3/missions/{id}` with
the child's `expected_revision`, its new `parent_id`, and the new parent's
`expected_parent_revision`. Both ends must remain inside your permitted subtree.
The API rejects cycles, closed parents and scope or child-budget violations.
Do not move the mission that grants your own authority to expand that authority.

Use `cancel_assignment` to remove obsolete queued work from execution, preserving
unfinished obligations and the reason. `cancel_mission` is permitted only once
its descendant missions and active assignments are settled; it records a terminal
decision with a note instead of deleting history. `complete_mission` requires
no open descendant children and
evidence that its acceptance criteria hold. Reordering,
cancellation and completion use the target's current revision.

## Checkpoint On A Concrete External Event

After checkpointing useful work and settling external delivery receipts, request
`POST /api/v3/commands`:

```json
{"operation":"checkpoint_assignment","target_id":"<own-assignment-id>","expected_revision":4,"args":{"note":"Reconcile durable publication before claiming delivery","start_condition":{"version":1,"expression":{"op":"status_in","target":{"kind":"publication","id":"<publication-id>"},"values":["verified","failed","cancelled"]}}}}
```

End the provider turn after the request succeeds. The worker releases the slot;
the same assignment and provider context resume when ready. Optional `not_before`
sets a delay. Leave the unfinished obligation open. Failure/cancellation wakes
you for recovery, not to assume success.

The same mechanism can retain an integration or repair owner while an already
arranged external reviewer or child finishes. Use `status_in` on that assignment
with `completed`, `failed` and `cancelled`, or `all` over the prepared reviewers'
returned terminal conditions. Verify that the producer can run without your
retained workspace. Native children still require a live parent; a checkpoint
does not preserve an active native child after its parent execution ends.

Do not wait for speculative future feedback or an event your own session must
produce. A task scoped only to publishing a PR can finish at durable publication;
a task that owns the concrete integration step can checkpoint until its inputs
arrive. The retained workspace remains host-bound and can restrict admission.

`resume_assignment` for a completed assignment is operator-only and rejects a
workspace already reassigned to unfinished work. An ordinary maintainer should
prefer an existing active/checkpointed author; otherwise create one narrow repair
task with the old PR, findings and evidence rather than promising unsupported
automatic resumption.

## Recurrent Planners And Maintainers

Read the run's `automation` records and update the existing automation using
`defer_automation`; do not stack replacement planners. Its arguments can include
`not_before`, `start_condition`, `cooldown_seconds`, `enabled`, and `no_progress`.
For example, wait until the ready nonautomation queue falls below a threshold
and a necessary producer settles:

```json
{
  "version": 1,
  "expression": {
    "op": "all",
    "args": [
      {"op":"queue_below","run_id":"<run-id>","count":2},
      {"op":"status_in","target":{"kind":"assignment","id":"<producer-id>"},"values":["completed","failed","cancelled"]}
    ]
  }
}
```

A terminal producer condition wakes a planner to inspect either success or
failure; it does not assert that its artifact exists. Use `publication_verified`
for consumers that actually require published source. `queue_below` counts
ready pending nonautomation assignments, excluding the assignment being evaluated;
combine this signal with dependencies and a delay when the queue is blocked.
`no_progress: true` applies bounded exponential
deferral; omitting it preserves the empty-pass counter, and `no_progress: false`
explicitly resets that counter when useful work resumes. None of these choices
allows spare capacity to override the producer condition above. Automatic idle
reconsideration applies only to capacity conditions, with time guards retained;
external conditions must become true or be explicitly revised by an authorized
session with an explained scheduling decision.
A maintainer can use `forge_actionable_count` scoped to repository
IDs, issue/PR kinds and labels when waiting for reviewable work. For a recurring
phase maintainer, also set `origin_run_id` to the current run and `review_phase`
to its phase so another run's PRs cannot wake this queue. This excludes
pull requests with an unresolved current-head `changes_requested` decision.
A new head or a superseding approval by that reviewer can make it actionable.
This is a review-backlog condition, not a gate for explicitly assigned repair
or disagreement resolution. `forge_open_count` retains its literal open count.

When work is stuck, read `coordination.memory`, then inspect the original
objections and owner outcomes. Use `update_assignment`, `move_before` or
`defer_automation` to repair existing pending work before creating duplicates.
Identify who can produce every unlock event. Never make an assignment wait on
its own open obligation or create a circular wait between author and reviewer.
If reviewers disagree, name the disputed claim and commission a scoped source
check, counterexample or consumer experiment; retain the result and resulting
route decision. Do not relax mathematical acceptance requirements to end a loop.
Resolve a recovery decision with evidence and an owned action, or set a specific
external-event condition and explain the wait. The next session must be able to
tell what was expected and why it did or did not happen.

Resolve a parent obligation as handled only after a concrete durable child was
created. Scheduled handling requires a pending child; failed/cancelled owners
are rejected. Keep an integration obligation open when the parent still needs
to incorporate the result. Avoid cycles and self-delegation.
