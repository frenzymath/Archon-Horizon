# Milestone Preprocessing

> This guide describes explicitly retained `workflow: milestones` projects.
> New projects use agent-led `workflow: graph`: milestones are ordinary labeled
> nodes, and maintainers accept phase strategy without this strict baseline
> machinery. See [architecture](architecture.md#phase-workflows).

New projects use `workflow: milestones`. Existing projects remain `legacy` after
migration; a human administrator can enable milestones while the project has no
active, paused or draining runs. Enabling protection does not infer acceptance
from old labels or change a running baseline. It cannot be disabled by a catalog
edit. Direct library postprocessing keeps its existing workflow.

The [source and agent contract](../src/archon_horizon/pipeline/skills/operations/horizon-graph/references/milestones.md)
contains the layout and complete metadata examples. Each objective owns
`milestones/<Objective>/milestones.md`, public `Mi.lean` declarations, a final
endpoint and concrete `Definitions/` modules. Markdown nodes remain the graph;
`belongs_to` records milestone ownership and `children` records prerequisites.
The objective may be rewritten into dependency-ordered architectural steps.

## Review And Launch

1. Retrieve and register primary references with BibTeX and exact locators.
2. Merge a route PR selecting named milestones with cited rationale and useful
   granularity. Decomposition review is required.
3. Merge contract PRs after compilation, axiom inspection and current-head
   statement-fidelity, definitions, decomposition and library-api approvals.
   Finish with an integrated reviewed manifest covering the complete route.
4. Index the merged revision and approve the baseline in the objective dashboard.
   Initial approval requires a human; Horizon records the actual authenticated
   approver, verified source manifest, review gates and immutable artifacts.
5. Launch formalization separately with a mission linked to that objective and
   the approved snapshot. Grow the detailed proof graph beneath its milestones.

These are enforced gates, not status-label conventions. Missing route approval,
current-head verification, required specialist coverage, registered references or
baseline acceptance blocks the relevant action. Merge delivery rechecks coverage
and the inspected target base. Use merge commits so reviewed PR heads remain
ancestors of the integrated baseline.

The Statement/Proof table distinguishes accepted contracts from open, in-progress,
conditional and complete proofs. Proof status requires a trusted Comparator and
axiom receipt; a source claim alone cannot establish completion. Progress
checkboxes and helper graph additions do not invalidate the mathematical contract.
Contract or dependency-pin changes do invalidate that evidence.

## Build Host

Upgrade the API and worker installations together and apply the migration with
the normal explicit `horizon-pipeline migrate` operation. Install worker extras,
including PyYAML. Enable `milestone_checks: true` on a host with `lean_build`
configured and explicitly unrestricted harnesses. Container-only hosts need a
separate configured build host; enabling the checker never relaxes their sandbox.
Register ready roadmap and solution workspaces on that host. Configure their
repository IDs in `publication_remotes` and `publication_header_files` when the
required commits may be absent locally. Credentials stay on the host.

The worker advertises `milestone_verification: 1`. Workers or maintainers submit
`POST /api/v3/milestones/verifications` with `workspace_id`, `source_commit_oid`
and `base_commit_oid`. For proof checks also include `solution_workspace_id`,
`implementation_commit_oid` and a repository-relative `comparator_config` path.
The host builds temporary isolated checkouts under the managed disk-backed build
root; source workspaces and dirty edits are preserved. Jobs have expiring leases,
fenced results, bounded recovery and inspectable failure diagnostics. Shutdown
terminates the checker process group and leaves its lease recoverable.

Read job state at `GET /api/v3/milestones/verifications/{id}` and its completed
receipt at `GET /api/v3/milestones/checks/{id}`. Repeating a live or completed
request returns the same job; a failed request can be resubmitted. The host/CI
CLI `python -m archon_horizon.pipeline.worker.milestone_verify --help` supports
direct checks and canonical table rendering. A JSON artifact uploaded by an
agent is never accepted as host verification.

The roadmap must pin Lean and Lake dependencies. Definitions compile without
importing milestone targets, and all their declarations and target types are
audited for transitive axioms. Only `propext`, `Classical.choice` and `Quot.sound`
are permitted there; `sorryAx` is allowed only in open theorem proofs. Proof
checks require the exact reviewed challenge source, matching dependency pins,
separate challenge/solution modules, complete Comparator target/definition
coverage and a subsequent implementation axiom audit. Configure the project's
pinned Comparator and lean4export packages and their runtime prerequisites.

## Corrections And Rollout

A counterexample, source mismatch, missing bridge or useful generalization can
justify a correction PR. The strict contract panel applies even during
formalization. Accept a replacement with `previous_snapshot_id`, then issue
`adopt_roadmap_snapshot` for each affected run. A merged PR never silently
replaces an adopted baseline; changed objective intent requires human approval.

Apply or update preprocessing and formalization reviewer presets before enabling
an existing project. Existing revisioned descriptors and pinned live skill
bundles are deliberately not overwritten by source edits. Use new assignments
with the updated catalog. Project migration is a reviewed route/contract effort;
old `formally_stated` labels are insufficient evidence for the new baseline.
