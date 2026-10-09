# Architecture

Horizon has one control plane in `src/archon_horizon/pipeline`. The `horizon` and
`horizon-pipeline` commands invoke its CLI. The API serves `/api/v3` and the React
dashboard at `/pipeline`.

The [objective orchestration contract](design/objective-orchestration.md)
describes the design and migration boundaries for new objective-led runs.

## Code Layout

Python modules are grouped by responsibility. The package root retains the
CLI/API entrypoints, configuration, authentication and shared request contracts;
it is not a flat registry of every implementation module.

| Package under `pipeline/` | Responsibility |
| --- | --- |
| `review/` | Contracts, review packets, pinned invocations, reviewer identities and maintainer decisions |
| `integrations/` | Forgejo/Zulip access, browser identities and durable external delivery |
| `missions/` | Mission ownership, scope, conditions and transactional domain services |
| `execution/` | Queue admission, scheduler, execution events and coordination recovery |
| `dashboard/` | Scoped read models and operator activity presentation |
| `instructions/` | Instruction catalogs, prompt construction and bundle assembly |
| `persistence/` | PostgreSQL schema, transactions, record primitives and immutable blobs |
| `projects/` | Project setup, source documents, milestones, bibliography and search |
| `operations/` | Installation diagnostics, storage, health, tracing and supervision |
| `providers/` | Native event parsing, normalized observations and usage accounting |
| `worker/` | Host execution, local journals, publication, sandbox and managed builds |

Package initializers contain documentation rather than service imports. This
keeps shared contracts and worker startup usable without server or scientific
dependencies. Callers import the concrete module they need; grouping files
does not itself enforce a dependency boundary or remove existing coupling.

Skills, descriptors and Alembic history remain at `skills/`, `subagents/` and
`migrations/`. Their loaders use `pipeline._resources` so moving a Python module
does not change which assets it loads. The `roadmap_index` and `graph_progress`
module commands retain their original entrypoints. Existing pinned instruction bundles remain immutable. Database changes use
explicit migrations; the current schema is revision `0030_optional_subagent_limits`. Revision 0028 introduced
objective queues; 0029 enables ordinary graph planning and evidence-backed phase
transitions without requiring a milestone baseline; 0030 makes native subagent
limits optional while preserving existing explicit settings.

## Durable State

PostgreSQL stores projects, missions, runs, assignments, execution leases,
obligations, catalog revisions, and delivery state. SQLAlchemy Core manages
bounded connections and transactions; Alembic maintains the current schema.
Only an explicit operator command applies schema migrations. Immutable artifacts
hold larger evidence, provider data and pinned instruction bundles.

Missions form an ownership tree. Each child records its acceptance criteria,
delegation purpose and structured scope within its parent. Creating or moving a
child checks the parent revision and open-child budget in the same transaction.
Agent credentials confine mission mutations to the current assignment's subtree.
The mathematical dependency DAG is separate: a dependency is required evidence,
not ownership or a request to launch another agent.

Assignments retain the durable work and obligation ledger for one scoped role;
executions are leased attempts on those assignments. The runnable queue is a
projection of mission state, assignment order and conditions, run limits, leases
and physical capacity. It is not another mutable task list. Agents can adjust
permitted queued work through revision-checked commands, while the scheduler owns
admission and resource claims. Narrow contexts aid mathematical decisions; server
invariants prevent stale updates, cycles and unbounded delegation.

## Work And Maintenance

New launches use `orchestration: objective` and resolve a versioned roadmap
`objective_id`. A short root mission supplies integration ownership; each session
gets a distinct bounded mission. Work and Maintenance categories have independent
queue bounds, slots, session/token budgets, harness/model defaults and retry
policies. The role remains the permission boundary. Host/provider capacity is
shared and reserves maintenance room when possible; one-slot hosts can alternate.

The scheduler starts one bounded Work planner. When it starts, exactly one queued
successor is created under a different mission. Cadence starts at 120 seconds,
increases with unchanged passes, and pauses after three unchanged passes by
default. Meaningful objective, work, Forge or host admission changes wake idle
planning. Explicit pauses survive those changes. The planner preserves existing
owners, dispatches useful unowned work and diagnoses failures before repeating it.

An eligible roadmap/library PR or issue labelled `awaiting-review` has a durable
review demand with a generation and one maintenance owner. Polling and duplicate
webhooks reuse the owner. A worker can request a new review after a repair; stale
label updates are rebased or repaired against durable demand. A completed review
round can get a fresh owner for a later request. An interrupted round resumes its
existing owner and keeps failure history. Workers can request other bounded
maintenance decisions without gaining maintainer authority.

The effective goal is the mission, acceptance criteria and open Markdown ledger.
Comments, evidence and delegated owners remain structured. The dashboard puts
outstanding work first and collapses settled history. A session may finish after
faithful delegation; mission acceptance remains an integration decision. Removing
required criteria needs a maintainer. A worker cannot enqueue its own mission or
an exact whitespace-normalized copy under another name; semantic avoidance still
needs maintainer judgment.

Existing database rows keep `orchestration: legacy`; their automations, configured
specialist requirements and pinned catalogs are preserved. New legacy launches
must select that mode explicitly; fresh `orchestrated: true` launches are
rejected. Legacy refill/recovery loops do not run alongside
the objective planner for the same run.

### Operational Diagnosis

Any live worker or maintainer can read `agent context --view operations`.
It supplies bounded run activity, owners, admission reasons, exact-head review
blockers, trusted-job state and sanitized host health, with authorized dashboard
and record drilldowns. Shared capacity is aggregate; another project's records
and host administration credentials are not exposed.

A concrete anomaly can justify the native `orchestration-auditor` helper. It
returns observations, a diagnosis, proposed repair, responsible owner and the
evidence that would confirm recovery. The parent applies authorized changes.
The helper does not run builds, change queues or act as a permanent monitor.
Its read-only task is an instruction, not a separate authorization boundary.

A mission tree helps divide context and responsibility, but does not guarantee
that independently chosen interfaces fit together. The graph records cross-branch
dependencies; an integration owner and review provide the necessary global check.
Awareness of other sessions is available when it affects a decision, not an
obligation for every worker to monitor the whole run.

Pending operator instructions and delivery failures remain explicit notices.
The CLI surfaces bounded notices at API boundaries; reading is not disposition.
Lease loss and stop requests are enforced by the host independently of model
attention. This infrastructure remains necessary even with simpler agent roles.

## Phase Workflows

All phases use the same session and queue lifecycle, with concise phase skills.

| Phase | Intended outcome | Acceptance |
| --- | --- | --- |
| Preprocessing | Literature/BibTeX, concise objective and milestone decomposition, concrete definitions and Lean statement skeletons | Reviewed roadmap strategy and source evidence proportionate to the objective |
| Formalization | Complete requested outcomes in the workspace; propose graph and contract changes through roadmap PRs | Source-bound proof/dependency evidence and maintainer phase decision |
| Postprocessing | Separate graph namespace with provenance; `Postprocessing/` workspace files; useful destination-library contributions | Library PRs with checked axiom closure, current-source review and accepted integration outcomes |

New projects default to `workflow: graph`. A milestone is an ordinary node with
its `milestone` display label; agents choose the route, statements and decomposition.
Maintainers accept the phase strategy without a special compiler-certified planning
baseline. Existing `workflow: milestones` projects retain their strict policy;
a human can explicitly switch an idle project to `graph` through a revision-checked
project update. Active-run changes are rejected. No migration silently converts
accepted project contracts.

New policies make specialists discretionary. Required external policy, checks,
authority and unresolved findings still apply. Destination-library checks derive
changed modules from the exact base/head and audit every declaration's axiom
closure. Build/dependency changes expand that scope to all committed Lean modules.
The checker fails unsupported import layouts rather than omitting files. Roadmap
skeletons can contain admissions; destination library contributions cannot.

Maintainers use `accept_phase` with evidence. Automatic transitions wait for the
accepting session, physical stops and deliveries to settle. An interrupted accepting
session resumes before transition. `auto_advance: false` pauses for a human boundary.
A blueprint objective can request preprocessing alone. Final acceptance closes the
root only after its children and commitments are accounted for, then drains the run.

Natural-language coverage, good decomposition and mission satisfaction remain
judgments. A ledger can verify ownership and evidence links; it cannot establish
mathematical meaning. Lean receipts establish particular elaboration/axiom facts
at pinned inputs and do not replace semantic review.

## Workers And Integrations

Each worker maintains a local durable journal. Assignment leases fence stale
execution; provider thread state supports continuation. Git checkpoints and API
intents survive transport failures for reconciliation and retry. Local Lean build
slots, exact-source cache keys, and resource deadlines bound compilation work.
Definitive validation rejections with no possible applied effect become retained
diagnostics. Conflicts and uncertain network outcomes remain unresolved until
reconciled. The host fingerprints the actual unresolved journal entries across
executions; resolving a server-side wrapper does not reset that failure budget.
One stable journal obligation is reopened as needed, and unchanged unresolved
intents eventually fail the assignment with a repairable diagnostic instead of
creating an endless sequence of fresh blockers.
Unfinished retained sessions reserve their worktree even while checkpointed.
Capable workers provision separate worktrees from pinned local Git objects when
the reusable pool is exhausted, acknowledging preparation before provider launch.
Persistent roadmap checkouts for explicit compatibility milestone jobs are
prepared separately and verified with `verify-workspaces` before use. A
`preparing` database record alone neither clones a repository nor makes a local
checkout available. Cleanup must preserve registered live workspaces, or update
their availability before later admission.
Planning can reconsider unused admissible capacity while other sessions run,
without creating a second planner or bypassing task dependencies.

Integration credentials stay with their configured owner. Server connectors
deliver Forge and Zulip operations using durable identities and idempotency
markers. Review descriptors specify a perspective; worker/maintainer roles and
repository policy determine authority. Specialist approval alone cannot grant
permission to merge.

The dashboard uses authenticated read models, bounded pages, cached queries and
event invalidation. Run phase and execution status are separate facts. Search
indexes and local reference caches are rebuildable projections, not the source
of project truth.

See [setup](pipeline-setup.md) for deployment and recovery procedures, and
[the design specification](design/pipeline.md) for the intended domain model.

## Resource Admission And Recovery

Linux workers report available memory, effective cgroup memory/CPU quotas and
memory, CPU and I/O pressure where readable. Conservative configurable thresholds
pause new admissions without destroying active contexts. Unknown observations stay
unknown. Compiler concurrency is independent of agent slots; the default managed
build lane admits one heavy build at a time, with shared caches and bounded waits.
Native delegation has no Horizon cap by default and does not reserve primary
category or host slots. Shared provider quotas account for uncapped children as
they are observed. An explicit child cap reserves capacity against category, host
and provider limits before launch; that reservation survives uncertain stops.
New objective launches create an account outage guard when no provider quota is
configured. This guard has no normal concurrency cap; after a failure, recovery
allows one probe at a time until the account is healthy again.

Transient failures resume the same session with bounded jittered backoff. Recovery
attempts survive continuations and explicit recovery. Exhausted or configuration
failures preserve work with a visible pause reason. Shared provider-account circuits
open after repeated failures and require an operator diagnosis/reset. Unknown
physical stops hold reservations until confirmed; a timeout is not a stop receipt.

A maintainer can retire a settled generated checkout. The server protects suspended
owners, open commitments, unsettled publication and pinned phase inputs. The host
then requires a clean tree and publication of every local commit to the durable
visible branch. Cleanup is limited to one retired generated checkout per five-minute
pass; failures retain the directory and a diagnostic. Existing cache retention
removes only rebuildable leased artifacts and old native output, preserving sources.

## Lean Verification And Compatibility

General destination-library audits use `POST /api/v3/lean/verifications` with a
ready library workspace and exact head/base commits. Poll the job by ID, then
read its `check_id` at `/api/v3/lean/checks/{id}`. The host-only worker lane leases
jobs and fences stale results; a receipt does not grant merge authority. Worker
`lean_checks: true` advertises the generic lane. It requires the explicitly
configured unrestricted managed-build profile, independently of agent slots.

`projects/lean_checks.py` stores exact-source receipts and
`worker/lean_verify.py` performs generic audits without milestone parsing. Legacy
milestone routes, flags and immutable storage table names remain compatibility
aliases. They do not impose milestone policy on a graph project. Fresh
`orchestrated: true` launches are rejected; saved legacy automation profiles live
in `execution/legacy_automation.py` so historical runs remain readable.
