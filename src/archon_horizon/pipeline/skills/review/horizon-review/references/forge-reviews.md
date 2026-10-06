# Forge Review Protocol

## Prepare a Bounded Review

Use the supplied readiness request template first. If a field or route is
unclear, look up only that installed operation's schema, for example:

```sh
horizon-pipeline agent schema --section operations --name prepare_reviewer
horizon-pipeline agent schema --section operations --name attach_reviewer
horizon-pipeline agent schema --section operations --name reviewer_report
```

Other names in this section are `cancel_reviewer`, `prepare_reviewer_assignment`,
`forge_comment`, `forge_review`, `forge_merge`, `forge_change`, `forge_create`,
`forge_edit` and `milestone_verification`. The `create` section takes record kinds
such as `assignment`; `command_args` takes command names such as
`cancel_assignment`. Read-only query schemas use literal route templates in
`queries`, such as `GET /api/v3/forge-items/{identifier}/inspect`.

Read PR
details and discussions through `/api/v3/forge-items/{id}/inspect`, pinning
`expected_head_oid` for head-sensitive views. Read relevant earlier reviews and
replies before creating new discussion. Respect provider pagination and view
limits; an excerpt is not the complete conversation.

Select reviewers with [review selection](review-selection.md) when the required
perspectives are unclear. Respect required policy dimensions and invoke extra
perspectives only for a concrete question. A library audit without a real Forge item
uses `library-audit` and ordinary delegation rather than this invocation API.

Put the bounded review question and relevant earlier findings in the preparation
instructions. A separate PR comment is useful when other participants need an
explanation or a decision, not mandatory duplicate dispatch paperwork.

The live maintainer prepares a reviewer with
`POST /api/v3/reviewer-assignments` for `invocation: assignment`, or
`POST /api/v3/reviewer-invocations` for `invocation: subrequest`, supplying `parent_request_id` from
`HORIZON_PROVIDER_REQUEST_ID`, `forge_item_id`, `reviewer_descriptor_id` and the
PR's `expected_head_oid`. Give any narrow additional question in `instructions`.
The Forge list and inspect responses include `review_readiness`: current head,
required and missing dimensions, invalid provenance, blockers and `next_actions`.
Use its descriptor IDs and exact head to prepare missing specialist invocations.
For prepare and retry actions, start the request body from `request_template`,
then add the values named by `required_fields`. Use `schema_command` for optional
request fields. The surrounding dimensions, ownership and action metadata are
readiness descriptions and must stay outside the API request body.

### Durable Reviewer Assignments

For a descriptor configured with `invocation: assignment`, preparation returns the
queued `assignment`, immutable `manifest_artifact_id`, parent `obligation_id`,
and a `completion_condition` using the assignment's terminal statuses.
Preparing the same pinned work returns the same owner. No native spawn, attach
or wait call is needed. The scheduler admits reviewers through normal primary
slots and shared provider limits; queued reviews need no idle parent process.

Resolve the returned parent obligation as `handled` with
`resolution: {kind: delegated, assignment_ids: [assignment.id], note: ...}`.
Use the current obligation revision. Retain one integration owner: reuse an
existing pending owner, checkpoint the current owner when it still owns concrete
integration, or create one narrower integration assignment if neither exists.
Use `start_condition: {version: 1, expression: {op: all, args: [...]}}` with the
selected reviewers' `completion_condition.expression` values as its arguments.
The condition includes failed/cancelled attempts so the owner can diagnose and
arrange an explicit repaired retry. A checkpoint releases the slot with the
integration obligation open. A completed dispatch pass settles its own work and
hands off integration. Leave the mathematical mission open until its actual
acceptance criteria hold. See [checkpoint mechanics](../../../operations/horizon-delegation/references/queue.md).

Each reviewer uses its own `HORIZON_PROVIDER_REQUEST_ID` and credential to publish
the pinned assessment, verifies its delivery receipt, resolves its own deliverable
obligation with that evidence, and calls `complete_mission` for its narrow review
mission before returning. A delivered changes request can complete a review
mission: author repairs and final acceptance belong to integration. Do not await
the PR's acceptance or your own readiness gate. If publication cannot be settled,
preserve its actual blocker and recovery ownership instead of claiming completion.
The integration maintainer checks each actual result, delivery, and current-head
policy gate; completed assignments alone do not imply approval.

### Native Reviewer Children

Keep the returned Horizon `provider_request_id` paired with its PR, head and
descriptor. Complete this sequence for each prepared native reviewer:

1. Invoke the provider's actual native spawn tool with the returned `prompt`
   verbatim and its model settings. Preserve the exact `Review label: hz_review_...`
   line, including its returned value. Pass `task_name` as a tool argument only
   when the installed native tool supports that argument. Preparation reserves
   the review; it does not start a child.
2. Read the actual child ID from the native tool's result. Use that provider ID
   as `native_key` in `POST /api/v3/reviewer-invocations/{provider_request_id}/attach`.
   For example, use the returned `agent_id` or child thread ID. Never use the
   `hz_review_...` task label as `native_key`. Supply `native_invocation_id` only
   when the provider returned that separate call identity. `/attach` records a
   launched child's identity; it does not launch the reviewer.
3. Keep the parent request and execution active. Collect or wait for that actual
   native child using its returned ID. The reviewer must publish its independent
   assessment through the prepared invocation's `/report` endpoint and return
   the operation receipt. Verify delivery and, after the native child finishes,
   the resulting review coverage.
   A spawn acknowledgment, pending reservation, successful attachment or empty
   wait response is not a delivered assessment.
4. Finish the parent review pass only after its launched children have reported
   and their deliveries are accounted for. Do not end or checkpoint the parent
   while a required child is still working; ending it can interrupt the child.

If launch or attachment fails, preserve the prepared request ID and the actual
native child ID, inspect the error and reconcile that same pair. Never replace
an actual child ID with the task label to bypass an attachment conflict. Cancel
an unused, unlaunched reservation through `/cancel`; reconcile a launched child
and preserve any real findings before arranging a replacement. A failed or
interrupted child remains incomplete and cannot be converted into an approval.

Use native reviewers within the maintainer's provider capacity. If another
harness is needed, a scoped maintainer on a capable host can own that PR and
launch its reviewers. Durable mode is configured before preparing work; it is
not an automatic fallback for full native capacity or a missing completion
event. Do not replace a missing native
result with an invented assessment or silently rerun its already published work.

## Publish Attributed, Useful Discussion

The PR is the cumulative review record. Publish every substantive reviewer
assessment, including clean reviews and findings the maintainer disagrees with.
A maintainer summary, private artifact or Zulip handoff does not replace the
reviewer's assessment. Read all earlier review rounds, inline findings and replies
before selecting another reviewer. Carry unresolved findings forward with links;
commission new review for uncovered risks or changed evidence, not to restart
the review history. Follow `horizon-communication` for the published text.

The reviewer publishes its own assessment through
`POST /api/v3/reviewer-invocations/{provider_request_id}/report`, with `verdict`,
`summary`, the prepared packet's required typed `assessment`, and any applicable
`comments` or `historical` flag. A planned approval requires the typed assessment;
use the packet's template and resolve the reviewer's own prior findings by exact
ID. The prepared manifest supplies
the reviewed commit and descriptor; the delivery uses that reviewer's configured
Forge identity. Native children use their inherited execution credential;
durable reviewer assignments use their own execution credential. Publication
does not require pretending the provider process has already completed. Verify
the outbox delivery receipt, return its link to the integration owner, and finish
the prepared lifecycle above. Do not wait for your own approval/readiness gate: independent
coverage requires the provider's actual completion event. A delivered report or
amendment does not release native capacity while the reviewer is still active.
Amend through that invocation only while its reviewer provider turn is active.
After it returns, additional reviewer work needs an explicitly authorized
reservation; in native mode, the parent execution remaining live is insufficient.
The maintainer checks the completed invocation and delivery before integrating
the findings. Recovery by the parent preserves the actual report and its attribution;
it does not replace the review with a maintainer paraphrase.

Use the connector's supported structured inline-comment fields for
findings about specific changed lines. Use a review summary for cross-cutting
design issues and the overall verdict. Consult the current schema for accepted
line coordinates; do not guess a diff position or attach an old finding to a
new line after a push. If inline discussion is unavailable, include stable
file/declaration links and report the limitation.

If the PR has changed head/base or closed, or you are recovering an incomplete
assessment rather than publishing your own completed review, publish the actual
available findings with `historical: true` and
`verdict: commented`, keeping its original invocation ID and reviewed revision.
Historical feedback cannot contain inline coordinates; put stable file/line
references in its summary. Explain incomplete coverage and retain the original
findings, including disagreements. This does not complete the invocation or
approve a new revision. Do not mark a reviewer completed merely to publish it.

Keep the descriptor and reviewed commit identifiable in a concise assessment.
The server may add canonical attribution. Verify the
configured reviewer account before publication; a maintainer-authored summary
is not an equivalent substitute for the reviewer's own public assessment.

The maintainer environment's `HORIZON_REVIEWER_ACCOUNTS_FILE` maps configured
descriptors to their authorized native accounts. A retained context without that
file uses `export HORIZON_REVIEWER_ACCOUNTS_FILE="$(horizon-pipeline agent reviewer-accounts)"`.
The helper writes a private file and prints only its path; credentials must never
enter retained tool output.
The scoped report endpoint is the normal durable path.
Use the selected reviewer's account for an authorized direct Forge discussion
operation not supported by that endpoint; do not use another reviewer's account
or the maintainer's final-approval identity. Do not print credential values in
tool output, prompts, artifacts, discussions or receipts. Return the published
URL and relevant discussion outcome. Role, identity and merge authority remain
separate; a specialist assessment cannot merge a PR.

Horizon projects review lifecycle labels through the policy's maintainer account,
or the configured integration account when none is selected. Reviewer accounts
retain read-only repository collaborator access; do not use them to write labels
or request code-write privileges to repair a label delivery. This does not change
the specialist attribution required for substantive assessments.

For each substantive finding, state the location, consequence, evidence and
requested change. The current connector publishes reviews with inline comments;
it does not expose arbitrary nested-thread replies or resolve-thread commands.
Read existing discussions and link a follow-up assessment to the original finding
instead of posting the same issue as a new discovery. When direct discussion is
unavailable, return the proposed reply and its target to the maintainer. Never
claim that a remote thread was replied to or resolved when that operation was
not performed. Direct native discussion, when authorized and supported by the
Forge, uses the correct account as described above; retain its URL as evidence.
Treat a finding as substantively addressed only after inspecting the revision or reason
that answers it, not merely an acknowledgment.

Publish one coherent assessment per completed review round. Use `commented` for
questions or an explicitly incomplete assessment, `changes_requested` for
substantiated blockers, and `approved` only for the inspected scope when evidence
supports it. A stale or incomplete review cannot approve the current head. Before
merging, the maintainer checks the exact current head, outstanding findings,
required checks and policy, then records its own acceptance decision.

The CLI journals mutation intent. After an uncertain response, reconcile the
same idempotency key and operation receipt instead of posting a second review.
An enqueued publication is not proof that the Forge received it. Wait for or
durably account for delivery before claiming that reviewers have reported.

## Reuse Unchanged Review Evidence

For a later head of the same PR, inspect the diff and relevant dependencies
before choosing fresh review. The maintainer's approved
`POST /api/v3/forge/review` may include `carry_forward` entries with
`source_review_id` and `evidence_artifact_id`; inspect the installed schema for
the complete current contract. This records the maintainer's reuse decision,
not a new specialist review or permission to skip uncovered dimensions.

Each immutable project blob has `kind: "review_carry_forward"`, `version: 1`,
`forge_item_id`, `source_review_id`, `source_commit_oid`, `head_commit_oid`,
`target_branch`, `base_commit_oid`, relevant `scope_paths`, `delta_analysis`,
`dependency_analysis` and `rationale`. Keep analysis specific to why that
specialist's conclusions remain valid; a list of unchanged filenames alone is
insufficient if changed imports or definitions affect their meaning. Link the
original public review and the evidence in a concise acceptance explanation.

Use only an eligible completed specialist approval for this PR and the current
rubric/policy. Unresolved later findings, changed statements or definitions,
relevant dependency changes, or missing trustworthy completion evidence require
fresh applicable review. A new PR starts with its own coverage; an old review
of similar code is background evidence, not an automatic approval. The control
plane checks eligibility and still binds acceptance to the current head/base.

## Make Review State Visible

Use the supported review-state labels to show which descriptors were selected
and their current outcomes with `review/<descriptor>/<status>`: `running`, `ok`,
`requesting-changes`, `commented`, `historical` or `cancelled` as applicable.
The invocation and delivered review are authoritative; a label is a projection
for scanning, not a replacement for the report or an automatic dispatch request.
Direct maintainer rubric assessments without a completed specialist invocation
are published as comments, even when their local rubric verdict is positive.
They do not earn an `ok` label or satisfy the independent panel. If native launch
is unavailable, hand the review to a capable maintainer and preserve the blocker.
Do not repeatedly publish rubric approvals to try to satisfy missing coverage.

Final maintainer approval is rejected before publication with `review_gate_blocked`
when readiness is incomplete. Fix its structured blockers first. Contract review
also needs an accepted, merged ancestor route covering those milestones. A race
after queuing can still invalidate readiness: a completed delivery receipt means
Forge received the review; `payload.gate_result.status` separately reports
`accepted` or `blocked`, with the current readiness and accepted gate ID when
applicable. Verify that result before claiming acceptance or requesting merge.
An older approval remains in the discussion after a new head, but must not be
presented as approval of unreviewed changes. Read the full history behind a label
before deciding what requires another review.
