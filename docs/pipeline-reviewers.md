# Skills And Reviewer Workflow

## Structured Assessments And Shadow Plans

The reviewer report endpoint accepts `assessment` instead of a Markdown `summary`.
Version 1 records `scope`, `dimensions`, `complete`, `classification_confirmed`,
`evidence`, `limitations`, `findings`, and `resolutions`. The server renders a
concise public assessment; typed reports do not need prescribed Markdown headings.
Legacy summaries remain accepted so retained sessions can publish their evidence.

Dimensions are `mathematical_contract`, `public_design`, `proof_trust`,
`computational_cost`, `distributability`, and `scholarly_traceability`. They are
questions, not reviewer accounts. A finding records a stable report-local `key`,
dimension, severity (`blocking` or `improvement`), scope, finding, evidence and
requested change. Approval requires a complete assessment without blocking findings.

An incomplete or change-requesting review never satisfies specialist coverage.
The latest current-head assessment controls coverage; old-head change requests
remain unresolved after a push. A resolution names the prior `review_id`, a
disposition (`repaired` or `withdrawn`), explanation and evidence. The original
reviewer must publish that confirmation through a prepared invocation at the
current head. The maintainer can implement the repair but cannot self-waive its
independent assessment. Resolution becomes effective after Forge delivery and
actual reviewer completion;
policy, rubric or target changes invalidate stale evidence. A valid explicit
carry-forward can retain a previously confirmed resolution together with its
unchanged reviewed scope. This first protocol supports the original reviewer;
delegating adjudication to a different persona is not yet supported.

Reviewer preparation optionally accepts `review_plan` with `version: 1`,
`mode: "shadow"`, `base_commit_oid`, `scope_paths`, `questions` (dimension and
question), and `risk_triggers`. It pins selected questions into the invocation
manifest alongside the head, policy and descriptor revisions. The independent
reviewer must confirm the classification and assess all selected questions to
approve; it can broaden the assessment when necessary. Delivery verifies the
target base, and merge refuses stale or inconsistent bases.

Shadow plans do not reduce any repository's required reviewer panel. Activating a
proportional dimension policy requires a separately calibrated implementation;
passing a plan cannot waive existing phase or repository requirements. No active
project descriptor, policy or retained catalog is rewritten by installing these
contracts. Administrative rollout must account for older unresolved reviews.

The current runtime is `src/archon_horizon/pipeline`. Its source skills are in
`pipeline/skills/{operations,lean,review}/`, and specialist reviewer descriptions
are in `pipeline/subagents/reviewers/`. Worker specialties occupy the sibling
`implementation/`, `research/`, `validation/` and `planning/` folders.
They ship together in the immutable
instruction bundle, but a reviewer is a subagent descriptor, not a skill.

## Skill Discovery

Each skill has a short frontmatter description and `metadata.category`:
`operations`, `lean`, or `review`, matching its source directory. The
control plane builds a grouped `SKILLS.md` index inside the immutable bundle;
initial prompts expose descriptions, while agents load the relevant full skill
only when needed. Continuation keeps the existing context and pinned bundle.
Operator additions/overrides use `skill_source_root` in server configuration.
Override the existing category/name path to replace a skill; duplicate names
at different paths are rejected. A session resolves its entrypoint from its
pinned index, so reorganizing source does not invalidate retained contexts.

The operations catalog covers startup, delegation, workspaces, Forge, Zulip,
roadmaps, obligations, progress, efficiency, and source research. Lean skills
cover search, builds, repair, refactoring, profiling, proof minimization,
simplification, counterexamples, and tactics. Review procedures cover statement
alignment, definition quality, proof review, and the maintainer workflow.

The entrypoint links a bundled harness orientation: phase semantics, durable
ownership, continuation and where authoritative evidence lives. Agents need no
checkout of Horizon's development docs. `horizon-communication` provides shared
Markdown, mathematical notation, semantic emoji and low-noise message conventions.
Markers are presentation, not database statuses or notification commands.

Selected Lean skills adapt MIT-licensed material from `cameronfreer/lean4-skills`.
Pinned provenance and the license ship in `skills/_sources/lean4-skills/`.
They require no plugin runtime, extra installation, hooks, or model provider.
Cameron's upstream repository combines a central skill with many references and
commands; top-level skill counts alone do not measure retained guidance.

Administrators can browse installed skills and their supporting files under
Agents > Skills, and source descriptors under Agents > Subagent library.
Agents > Reviewers edits the descriptors enabled for the selected project.
These are distinct views: installed templates do not imply enabled reviewers.
Prepared reviewer invocations inject the selected revision's instructions and
exact PR head directly. Their relevant skills remain available on demand;
they do not rely on the reviewer discovering its own persona in a skill.

Preparation includes an exact-head review packet whose ASCII JSON representation
is bounded to 16 KiB. It contains the selected policy and rubric, reporting
contract, trusted-check summary, and exact evidence URLs with their response
fields. Complete instructions and the typed report schema remain in an immutable
context artifact; oversized sections have explicit omission markers and JSON
pointers. Reviewers start from this packet, read omitted requirements, and fetch
deeper skills or evidence for concrete questions. They still assess semantics
independently and recheck relevant discussion and head/base pins before reporting.

The report response's `id` is its outbox UUID; its `idempotency_key` identifies
the API receipt. Check delivery through
`GET /api/v3/operations/{idempotency_key}?operation=reviewer_report`, even though
the returned outbox `kind` is `forge_review`. A completed receipt with
`result_ref_id` establishes delivery. It does not finish the provider turn or
establish acceptance of the PR.

## Worker Specialists

Nine optional templates support bounded native delegation:

| Category | Descriptors |
| --- | --- |
| Implementation | `lean-worker`, `refactorer`, `forge-integrator` |
| Research | `source-researcher`, `page-transcriber` |
| Validation | `build-checker`, `debug`, `library-auditor` |
| Planning | `graph-planner` |

The pinned `SUBAGENTS.md` index exposes names, descriptions and file paths.
Initial assignment prompts include the worker-specialist summaries; continuation
does not reload them. The parent reads a selected descriptor and includes its
body, task, exact inputs, editing scope, skill-directory path and expected result
in the native child prompt. The provider's actual subagent tool launches the
child; a descriptor file alone does not register a native agent type.

Workers can use specialists without becoming maintainers. Parent sessions retain
integration and ledger ownership, and respect provider capacity. Native children
are not durable future assignments. When work must outlive the parent, use the
ordinary assignment queue with the selected instructions and a concrete mission;
specialist names are not entries in the assignment `functions` field.

`graph-planner` addresses mathematical decomposition. Run scheduling remains the
existing worker planner function. The former collaborator and maintainer profiles
are represented by the two execution roles; this catalog adds no new permission
profiles, automatic dispatch loops or accounts. Formal PR reviewers continue to
use the prepared invocation and attribution workflow described below.

## Review Perspectives

| Destination And Phase | Available Perspectives | Intended Scrutiny |
| --- | --- | --- |
| Roadmap, preprocessing | Statement fidelity, decomposition, definitions, library API | Route selection first, then strict compiled contracts and concrete definitions before human baseline approval |
| Roadmap, formalization | Roadmap consistency; full contract panel for corrections | Proportionate graph/progress review; strict review for frozen contract changes |
| Library, postprocessing | Mathematical fidelity, library API, library architecture, Lean proof quality, Lean performance, repository quality, scholarly quality | Strict, starting with statements and definitions, then proofs and publishing quality |
| Workspace | None by default | Free working space; destination policies apply when promoting code |

Installed templates alone do not impose a panel. For postprocessing, however,
every enabled descriptor linked to the repository policy must provide a
current-head receipt before merging; a maintainer-only decision cannot replace
it. Milestone projects enforce decomposition approval for route PRs and all four
contract perspectives for Lean contract PRs, with a trusted current-head receipt.
This also applies to contract corrections during formalization. Legacy projects
and verified graph-only changes retain proportionate selection. The maintainer
can repair bounded issues directly and manage several PRs at once. Independent
reviewers use fresh contexts and the exact PR head. A statement
or API defect may make proof polishing premature; independent concerns and PRs
can run in parallel within the existing provider capacity. The common review
contract distinguishes blockers, useful improvements and preferences. Reviewers
must return concrete evidence, not manufacture objections to appear strict.

Before launching a round, the maintainer posts its brief review intent on the PR:
selected perspectives, concrete questions, relevant prior coverage and integration
owner. New project review presets use durable reviewer assignments, configured
with `invocation: assignment`. Each selected perspective gets a narrow mission,
an independently leased worker and an immutable exact-head review manifest.
Reviewers consume primary slots only when dispatched; the dispatching maintainer
can finish after delegating its obligations and queueing a terminal-conditioned
integration maintainer. Native subrequests remain an explicit option for short
reviews collected during one parent execution. Existing descriptors and pinned
invocations keep their mode; missing native completion events must be reconciled,
not bypassed by duplicating already owned work in another mode.

For a durable review, call `POST /api/v3/reviewer-assignments` with the same
`ReviewerPrepare` fields as native preparation. The response contains the queued
assignment, pinned manifest, parent obligation, and `completion_condition`.
Delegate that obligation explicitly to the returned assignment; combine all
selected completion expressions with `op: all` in one integration assignment's
start condition. Completed, failed and cancelled reviewers all wake integration,
which inspects their receipts and current-head coverage before approving or
arranging a diagnosed retry. The reviewer publishes using its own current
provider request ID, settles its own obligation with the delivered review,
completes its narrow mission, and returns. It does not wait for authors or the
gate that requires its own completion.

A completed reviewer with an unusable report can receive an explicit correction
on unchanged pins when `review_readiness.next_actions` identifies that defect.
Submit the supplied `retry_of` and a concrete `retry_note`. The service preserves
the reviewer identity, head, policy and review plan, and checks that the previous
process and deliveries are settled. Valid approvals and substantive change
requests cannot be rerun to seek a different verdict. Old reports remain visible.
Review packets include the reviewer's earlier objections; approving a repair
requires explicit evidence-backed resolutions, not just a new approval label.

Preset recipes accept `invocation: subrequest` to opt into native mode. For an
existing project, revise a descriptor through the supported catalog API before
its first preparation on the intended head; a descriptor revision change can
invalidate previously pinned coverage. Do not rewrite an active invocation's
manifest or switch its mode to recover a missing event.

Use the PR's `review_readiness` to identify missing current-head coverage and
existing review work before preparing another invocation. Its `next_actions`
provide the exact schema lookup and accepted request fields; diagnostic metadata
is not part of the preparation request. For a new native invocation, preserve
the full prepared prompt, including its exact `Review label`, in the actual
subagent launch. Attach the child ID returned by the provider. The review label
identifies the task; it is not a native child ID. Attachment registers a launch
that has already happened and does not start a reviewer itself.

In native mode, keep the parent active until its reviewers finish and their independent reports
are delivered. Reviewers verify their outbox delivery receipt, return it to the
maintainer and finish their native turn. They must not wait for their own
approval/readiness gate, which requires actual native completion. Delivered
reports and amendments retain their evidence without closing provider requests
or releasing capacity; native terminal observations or confirmed execution stops
control that lifecycle. A confirmed stop without a native result records an
interruption, not a successful review. Lease loss alone, however old, retains
capacity until the host confirms cleanup or an operator records physical fencing.
A child observed before attachment can be reconciled only when
its live execution, parent and pinned environment match the prepared invocation.
Interrupted or ambiguous histories remain explicit repair work. Confirm both
the delivered report and the gate result: a successfully published comment alone
does not establish review acceptance. Retries reference the interrupted attempt
and explain the repair; existing active work is collected rather than duplicated.

Prepared prompts include a bounded current-head verification discovery URL,
plus any matching pending verification job and existing trusted receipt. A job
can finish after preparation: refresh the published evidence at a useful review
boundary before reporting a missing check. The check ID and job ID name different
endpoints. Reuse evidence only when its repository, source/base commits, manifest
and toolchain apply; a successful build does not establish semantic correctness.
Do not duplicate a full build while matching trusted verification is pending or
passed without a specific uncovered concern. Necessary focused probes belong in
reviewer-owned disk-backed `$TMPDIR`, never in host-managed job checkouts or caches.

Selection is adaptive: start with the questions that matter, inspect reports,
then add perspectives for material remaining uncertainty. The catalog groups
reuse/naming/generality under library API, packaging/imports under repository
quality, and attribution/exposition under scholarly quality. Projects can split
or combine these in custom revisioned descriptors. Explicit destination policy
requirements still apply; the installed list itself imposes no review quota.
Postprocessing policy links define the mandatory panel; maintainers can also
select any enabled project descriptor for an additional relevant concern. Register a custom
descriptor in the project before preparing its attributed invocation.

The shared contract requires coverage of significant changed declarations in
each perspective, concrete positive checks, severity-calibrated findings and
exact evidence. Public reports lead with the decision and actionable findings;
a clean approval is short and links its detailed coverage/check evidence. Scope,
blocking findings, verified checks, validation limits and verdict may be combined
instead of repeated as long sections. Follow-ups inspect affected findings and
the new delta, linking unchanged evidence while retaining required current-head
receipts. Direct maintainer reviews
use the same evidence standards. Source templates receive the shared contract
through the loader; existing project descriptors must be updated explicitly.

The detailed phase workflow ships in
`horizon-pipeline/references/phases.md`, available to workers and maintainers.
For already proved input, postprocessing normally adapts coherent result families
with their proofs. Review public statements and definitions first within those
PRs. A separate prototype is useful for materially uncertain interfaces, not a
mandatory stage for every port. Prototypes may contain explicit `sorry`: the README, or
a prominently linked proof ledger, records each declaration, accepted statement
PR, proof status, dependencies and durable owner. Subsequent PRs progressively
prove those milestones and explain the contribution's consumer path. Statement
acceptance and proof closure are distinct claims; library review is strict about
the former immediately without requiring the latter prematurely. Reviewers can
ask for strategy and design discussion even when code elaborates, comparing
relevant mathlib declarations and proposals before accepting a competing API.

Postprocessing requires bounded, low-cost improvements to useful generality,
public signatures, definitions, naming and structure. A current consumer passing
does not justify accidental specialization, duplicate infrastructure or avoidable
layers. Reviewers identify the concrete benefit and tested alternative; the
maintainer repairs it directly or records a specific tradeoff before acceptance.
Speculative generalization and personal preferences remain distinct. Public
design takes priority over proof micro-optimization.

When the workspace already supplies the proofs, workers port and adapt independent
proof groups in parallel against pinned source and destination interfaces. Separate
file ownership and a shared integration owner prevent collisions. New research
needs an identified source gap or concrete simplification benefit; the campaign
should not routinely reprove existing results. Statement and definition review
remains strict, and destination proof closure must be verified independently of
the source's completed status.

The PR contains the cumulative review history. Maintainers read earlier reviews,
inline findings and replies before choosing additional perspectives. Each
substantive specialist assessment is published through the durable Forge review
operation by the reviewer itself, including clean assessments and disagreements.
The maintainer tracks publication for native and durable reviewers and verifies
the receipt; a private report or its own approval is not a substitute. The
semantic emoji and bold labels from `horizon-communication` distinguish scope,
blockers, improvements, evidence and verdict. Review-state labels summarize the
selected descriptor's running, accepted, change-requested or other outcome while
the PR discussion preserves every round and unresolved finding.

Lifecycle labels use the policy's maintainer Forge identity, or the configured
integration account when no maintainer identity is selected. Reviewer accounts
retain read-only repository collaborator access; their substantive assessments
still publish under the selected specialist identity. Label history therefore
records control-plane projection, while review records preserve authorship.

Previously queued label intents retain their original credential choice. A
definitive HTTP 403 from a specialist-owned label must not be retried unchanged:
cancel that failed or pending intent, then queue the current label state with a
new idempotency key through the operational identity. Reconcile running or
uncertain deliveries first, and leave already-cancelled labels cancelled.

For a superseded head, changed base, closed PR or recovered incomplete assessment,
submit `historical: true` and `verdict: commented` against the original invocation.
Its manifest retains the reviewed commit. Inline coordinates are prohibited in this mode; use stable file references
in the summary. Horizon preserves pinned rubric provenance and labels lifecycle
uncertainty. This publishes feedback without completing the invocation or granting
approval. Historical reports use the descriptor's current publishing identity;
current approvals retain the identity pinned when the reviewer was prepared.
Setting a descriptor's identity alone does not authorize an external account;
its native permissions and credential must be provisioned and verified.

## Library Audits And Tools

`library-architecture` examines composition across modules and PRs: canonical
objects, bridge lemmas, instances, simplification, dependency direction and
constructed inputs. `library-audit` provides the broader workflow when accepted
PRs may be locally sound but collectively problematic. The maintainer pins an
accepted commit, subsystem, concrete doubt and representative consumers, then
selects specialists as the evidence warrants. Sampling is explicit. Findings
identify affected endpoints, bounded repairs, an integration owner and checks
on the repaired composition.

An audit with no open PR uses an ordinary native child or durable assignment,
not a fabricated reviewer invocation. The parent records evidence and owned
repair issues/assignments. Audits do not automatically block unrelated PRs,
rewrite accepted code or run periodically without a mission/trigger.

The bundled review tool guide covers source/search, Lean probes, trust checks,
consumer builds and Radar/CI performance evidence. Radar is a separately
configured benchmark server/runner service; Horizon does not install one or
assume its GitHub commands work on Forgejo. Existing configured benchmark
requests follow their project authorization and durable Forge delivery path.

`lean-performance/scripts/compare_measurements.py` is a read-only, standard-library
Python helper packaged with the skill. It compares existing Radar-format JSONL
artifacts, sums duplicate metrics with matching units, reports missing metrics
and unit changes, and leaves percentage change unknown for a zero baseline.
It does not authenticate caller-supplied provenance, launch benchmarks, publish
results or impose a performance threshold. The tool guide documents invocation
and comparability requirements. New Radar deployments remain operator setup.

## Authorship And Native Accounts

Execution roles remain `worker` and `maintainer`. A reviewer descriptor supplies
perspective, instructions and optional model settings; it does not create a new
permission role. Prepared invocations pin the descriptor, repository, phase,
commit and selected integration identity. Their lifecycle is tracked independently
from the parent's other reviews.

Configured reviewer descriptors use their own Forge accounts through
`integration_identity_id`; the maintainer account owns its own assessment and
final merge decision. Invocation-backed results preserve their descriptor,
rubric revision and reviewed commit through the durable delivery queue.
Project setup must provision and link the selected accounts, reusing an existing
identity when applicable. A template's presence in the catalog alone does not
launch a reviewer or grant repository permissions.

The maintainer receives `HORIZON_REVIEWER_ACCOUNTS_FILE`, a private descriptor
account mapping for authorized direct Forge interactions. Retained executions
can retrieve the mapping through their scoped reviewer-accounts API directly
into a private mode-0600 scratch file, without printing the JSON response. A reviewer
uses only its selected identity and never copies credential values into prompts,
logs or published evidence. Ordinary assessments use the reviewer-owned report
endpoint so that publication remains journaled and recoverable. Direct Forge
discussion operations can use the matching account where the scoped writer
does not yet expose the native operation; record their published URLs.

Zulip remains for decisions needing attention, with the existing configured
identity. Attribute a specialist discussion with a reviewer heading and a link to
the PR review; do not copy every finding or credential to chat.

## Reviews And Line Discussions

Read the PR description, diff, existing reviews and relevant replies first. Use
the Forge inspection API for `reviews` and `review_comments`, including pagination.
The reviewer submits its structured report to
`POST /api/v3/reviewer-invocations/{provider_request_id}/report`; the manifest
binds the descriptor, identity and reviewed revision. Native reviewers use their
inherited execution credential; durable reviewers use their own. Reporting is
separate from provider lifecycle completion and can carry line-specific findings:

```json
{
  "verdict": "changes_requested",
  "summary": "The public definition excludes a case required by the source.",
  "historical": false,
  "comments": [
    {
      "path": "Geometry/Curve.lean",
      "new_position": 18,
      "body": "This hypothesis excludes closed curves; compare the source definition."
    }
  ]
}
```

For the current reporting contract, use
`horizon-pipeline agent schema --section operations --name reviewer_report`.
Submit through `agent request` so idempotency and reconciliation are preserved.
If new evidence corrects an assessment while the native reviewer invocation is still active,
the same reviewer can amend it through the same invocation's `/report` endpoint.
Reconcile any uncertain previous delivery first; a changed assessment is a new
intent, not a replay of the old body. Explain the correction and explicitly
resolve earlier blocking findings. Once that native turn has returned, a live
parent execution alone does not authorize more reviewer work: arrange an
explicitly authorized reservation before resuming it. Use current API receipts and contracts for
this decision; old release source trees are not authoritative runtime evidence.

`new_position` and `old_position` are file line numbers on the corresponding diff
side; specify exactly one positive position. General observations belong in the
summary. Comments and the review are created in one native API operation, with
the same idempotency/reconciliation marker, so a lost response cannot create a
second review on retry. Reading native thread replies is supported; the current
writer creates review discussions, not arbitrary nested replies or resolve-thread
operations. Those capabilities must not be implied by a generic issue comment.

Only the maintainer makes the final policy decision. Specialist approvals remain
advisory and cannot independently open a merge gate. Recheck the current head and
required checks before merging; changed evidence can require a focused follow-up.

## Updating Existing Projects

Shipped preset changes affect new project recipes. Existing reviewer descriptors
are revisioned records, editable in Agents > Reviewers or through the catalog API.
Update their `instructions` and use `functions: ["reviewer"]`; rubric names belong
in the descriptor slug, not the execution function list. Preserve custom identity,
harness, model and policy choices. New invocations capture the updated revision;
old invocations and old provider contexts keep their pinned evidence.
