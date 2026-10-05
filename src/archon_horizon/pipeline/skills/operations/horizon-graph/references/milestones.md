# Milestone Contracts

This workflow applies when the project has `workflow: milestones`. New projects
default to it; legacy projects require an explicit human change while idle.

## Source Layout

Keep `nodes/`, `objectives/` and a buildable Lean package with pinned
`lean-toolchain` and `lake-manifest.json`. Use a Lake library with
`srcDir = "milestones"`; module names below are relative to that directory.

```text
objectives/example.md
nodes/example/m01.md
milestones/Example/milestones.md
milestones/Example/Definitions/Basic.lean
milestones/Example/M01.lean
milestones/Example/Endpoint.lean
```

An objective has ordinary roadmap frontmatter plus:

```yaml
milestones:
  root: milestones/Example
  endpoint:
    path: milestones/Example/Endpoint.lean
    module: Example.Endpoint
    declarations: [Example.endpoint]
```

Its concise body groups milestone checkboxes into dependency-ordered `## Step`
sections sharing an architecture. The initial route may omit `endpoint`.
Workers may reshape the human draft while preserving the mathematical goal.

Create a node alongside each milestone:

```yaml
label: example-m01
type: milestone
title: Normalized initial data
children: []
milestone:
  objective: objectives/example.md
  id: M01
  contract:
    path: milestones/Example/M01.lean
    module: Example.M01
    declarations: [Example.normalized_initial_data]
  definitions: [milestones/Example/Definitions/Basic.lean]
  references:
    - cite_key: primary-source
      locator: Section 2, Theorem 2.1
  statement: accepted
  proof: open
```

IDs such as M01 are unique within an objective; node labels are unique across the
roadmap. Every declaration has one locator. Shared definitions can live under
`milestones/Shared/Definitions/`; other objectives may import them. Keep imports
acyclic. `children` means actual logical prerequisites. A helper node can have
`belongs_to: [example-m01]` to record its milestone without inventing an edge.

For the route PR omit `contract`, use `statement: proposed`, and give the chosen
names, dependencies, source rationale and table. Do not start contract preparation
until the route PR is accepted. Balance useful mathematical boundaries across
the proof; no fixed milestone count or identical proof difficulty is required.
The maintainer and decomposition reviewer decide whether to split or combine.

## Contract Review

Each `Mi.lean` gives precise source locators, intentional generalizations and
dependency comments, imports its concrete definitions, and exposes the exact
public theorem expected in a Comparator solution. Its proof may be `by sorry`.
All definitions and the theorem's type closure must use only `propext`,
`Classical.choice`, `Quot.sound`; no hidden admission, extra axiom or circular
certificate field may substitute for the mathematics. Compile definitions and
statements together, checking constructible inputs and representative consumers.

Contract PRs require current-head specialist approvals from `statement-fidelity`,
`definitions`, `decomposition`, and `library-api`, plus the maintainer's decision.
Reviews address generality, source alignment, canonical definitions and the
combined route. Fix incompatible previously accepted neighbors when necessary.
Incremental PRs are allowed; finish with an integrated reviewed manifest covering
every active milestone and the endpoint. A source `statement: accepted` records
the intended post-merge state; the API only displays acceptance with review
evidence. Keep just Statement and Proof columns, not a redundant review column.

## Verification And Approval

Request the trusted host check through
`POST /api/v3/milestones/verifications`: supply a registered ready roadmap
`workspace_id`, exact `source_commit_oid` and current PR target `base_commit_oid`.
Inspect the returned job at `GET /api/v3/milestones/verifications/{id}` at work
boundaries. The configured host builds isolated pinned checkouts, publishes a
receipt and records `check_id` or an actionable failure. No host credential enters
the agent session. A failed job may be requested again after fixing its cause;
duplicate live or completed requests return the same job.

When only receipt reconciliation remains, queue one narrow follow-up or defer the
existing maintainer automation with this exact start condition, using the returned
job ID:

```json
{"version":1,"expression":{"op":"status_in","target":{"kind":"milestone_job","id":"<job-id>"},"values":["completed","failed"]}}
```

`milestone_job` is the trusted build's condition kind. Generic `verification`
records are a different object; this job has no `cancelled` state. Preserve the
same job and a real maintainer as integration owner. Wake on failure as well as
success, then inspect its receipt or error before deciding the next action.
Do not create timer-based polling children, relaunch a live build, or keep a
provider session waiting for it. A completed worker or lease deadline does not
mean the host build has finished.

The host requires `milestone_checks: true`, a managed Lean build policy and
explicitly unrestricted execution. Container-only hosts need a separate configured
build host. Register roadmap and solution checkouts there and configure their
`publication_remotes`/header files if requested commits are not present locally.
This capability is advertised in host health; a missing checker fails visibly.

The operator can also invoke the checker directly for a clean committed checkout:

```sh
python -m archon_horizon.pipeline.worker.milestone_verify \
  --worker-config /path/to/worker.json --workspace-id WORKSPACE_UUID \
  --root /registered/roadmap --base TARGET_COMMIT --publish
```

This direct command is a host/CI operation, not a command for an agent to run with host credentials.
The worker publishes an authenticated receipt at
`POST /api/v3/worker/milestone-checks`. An agent-written JSON artifact is not
verification. Arrange the configured checker before requesting a merge; the gate
fails closed when verification is absent. Agents can render tables without host
configuration or credentials using
`python -m archon_horizon.pipeline.worker.milestone_verify --root . --tables`.
It reads committed source; include the generated table in the final source PR.
Keep the registered checkout clean and pinned.

Inspect `GET /api/v3/documents/{id}/milestones` for checks, merged review gates,
statuses and approved baselines. Its `initial_baseline_readiness` diagnoses whether
a successful graph or contract receipt covers the exact indexed source commit
and manifest. A pre-merge PR receipt does not cover a different merged commit,
even when the contracts are unchanged. After accepted contract integration, follow
the bounded verification action with a registered ready roadmap workspace and
the exact comparison base; unchanged contracts can use the reviewed PR head as
that base. Consume the completed job's receipt before publishing the final packet.
During route or unfinished contract work, finish its review prerequisites first.
This diagnostic does not establish all acceptance requirements or grant approval.
After indexing and verifying the merged integrated revision,
the human approves via the objective dashboard or
`POST /api/v3/milestones/baselines`, with `document_id`, `expected_revision`,
`check_id`, `route_gate_id`, `contract_gate_id` and a decision `note`. The initial
approval requires a human. It writes immutable manifest/evidence artifacts and
an acceptance record. Generic snapshot creation cannot bypass this gate.
Formalization is launched separately against that snapshot and its objective's
mission. Do not launch proof work merely because the contract PR merged.

## Proof Work And Corrections

Build finer helper nodes beneath milestones as proofs develop. Preserve the frozen
challenge sources and supporting definitions; write implementations in a distinct
solution environment with identical public names/types. The trusted runner accepts
`--solution-root` and `--comparator-config` for a pinned Comparator check. For a
queued proof check, include `solution_workspace_id`, `implementation_commit_oid`
and the repository-relative `comparator_config` in the verification request. It
checks every milestone and endpoint, compares the definition surface, and audits
the implementation's axiom closure. Record its ID in `proof_check_id` and update
the table in a graph/progress PR. A direct admission is open, an admission-free
entry depending on lower admissions is conditional, and foundation-only closure
is complete. `in_progress` records ownership, not proof certification.

Correct a frozen statement only through a justified roadmap PR: show a concrete
counterexample, source discrepancy, missing bridge or useful generalization,
and trace the effect on other milestones. The same strict contract panel applies
during formalization. Accept a replacement baseline with `previous_snapshot_id`
and then explicitly `adopt_roadmap_snapshot` in each affected run. Changed human
intent needs human approval. A merge does not silently switch existing runs.
