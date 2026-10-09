# Core implementation review

The 2026-10-08 core pass read every executable body in the 39 files listed below:
422 named functions, including methods and nested functions, and 43 lambdas.
It also inspected module-level executable statements and declarations. The
[catalog](core-catalog.json) records every qualified callable, exact source span,
SHA-256 identity, observed responsibility, necessity, contract, invariants,
efficiency/documentation assessment and associated finding. Counts and hashes
index the review; they do not establish that a body was understood or executed.

The first pass used Python AST output with comments and standalone string
expressions removed. It retained executable strings, SQL constraints and field
metadata. The reviewer derived the
[independent implementation specification](../design/implementation-audit.md)
before consulting architecture, design or user documentation. Mandatory
repository/skill instructions were read separately. Source comments, docstrings
and relevant tests were assessed after that executable pass; repaired bodies
were read again. Dependent subsystems have separate scoped reviews.

## Resolved findings

| Finding | Observed defect and resulting behavior | Validation |
| --- | --- | --- |
| CORE-001 | Mission creation checked linked nodes/documents against scope, but an update could narrow the scope around existing links. `Service.update_mission` now rejects either exclusion with `scope_link_mismatch`, preserving the original scope. | Two regression cases exclude a linked node or document and check that the mission remains unchanged. |
| CORE-002 | Capacity diagnostics counted execution rows while admission reserved the parent plus native children. Pool occupancy now sums `1 + native_capacity`, so reported free slots agree with the reservation. | A live execution with two reserved native children occupies three of four slots. |
| CORE-003 | A graph objective could start without a baseline but could not adopt its first matching snapshot because the command required an existing baseline. Adoption now derives objective identity from the run/root roadmap when no prior baseline exists and retains the existing match checks. | A graph formalization objective without an initial baseline adopts its first matching frozen snapshot. |
| CORE-004 | Reopening a completed objective called the legacy replenisher, which rejects objective orchestration. Reopening now uses `objectives.ensure_successor` for objective runs. | Completing and reopening an objective reopens its root mission and creates exactly one pending planner successor. |
| CORE-005 | Valid JSON with a non-object root could raise Python shape errors during login or worker heartbeat. Both endpoints now reject those bodies with structured HTTP 422 errors. | Eleven parameterized cases cover null, boolean, numeric, string and array roots across the two endpoints. |
| CORE-006 | The command service allowed an objective worker to request a bounded maintenance decision, but the HTTP command gate required maintainer authority first. The HTTP gate now applies the existing worker exception to `request_maintenance`; the service still enforces objective ownership and evidence. | An execution credential requests maintenance through `/api/v3/commands` and receives the maintenance decision owner. |
| CORE-007 | Global health treated the expected active objective planner and its queued successor as duplicate automation. The threshold is now two for objective planners and one for legacy automation; the diagnostic exposes `objective_planner_max_outstanding = 2`. | A claimed planner with its single successor remains healthy with zero duplicate automations. |
| CORE-008 | Documentation strings after future imports were inert expressions in `models`, `persistence.database` and `persistence.schema`. They now occupy the module-docstring position; obsolete schema replacement wording was removed. | AST inspection confirms actual module docstrings in every scoped module; exact file and symbol hashes were rechecked. |
| CORE-009 | Important orchestration boundaries lacked concise purpose/contract documentation, and `read_git_snapshot` described a narrower input set than it reads. Added contract docstrings to API composition, command execution, mission update and scheduler claim/finish; corrected the snapshot description to include selected Lean build sources. | Source review compared the prose with executable control flow and selected source handling. No behavioral test was added for prose-only changes. |

Behavioral regressions live in
[`tests/test_pipeline_core_review.py`](../../tests/test_pipeline_core_review.py).
There are 18 executed cases after parameterization. CORE-008 and CORE-009 are
documentation repairs and are assessed by source/AST inspection.

## File coverage

Paths below are relative to `src/archon_horizon/`. The finding column includes
module findings and findings associated with contained functions. Every callable
has its own catalog entry, including thin HTTP adapters and inline callbacks.

| File | Named functions | Lambdas | Responsibility | Findings |
| --- | ---: | ---: | --- | --- |
| `__init__.py` | 0 | 0 | Lightweight package boundary | — |
| `__main__.py` | 0 | 0 | Canonical CLI module entrypoint | — |
| `pipeline/__init__.py` | 0 | 0 | Lightweight package boundary | — |
| `pipeline/_resources.py` | 0 | 0 | Packaged resource roots | — |
| `pipeline/api.py` | 153 | 34 | HTTP composition and transport boundaries | CORE-005, CORE-006, CORE-009 |
| `pipeline/auth.py` | 9 | 0 | Opaque authentication and live authorization | — |
| `pipeline/cli.py` | 10 | 1 | Explicit operator/worker/dispatched-agent entrypoints | — |
| `pipeline/command_args.py` | 1 | 0 | Shared lifecycle argument schemas | — |
| `pipeline/commands.py` | 3 | 0 | Revisioned lifecycle command dispatcher | CORE-003, CORE-004, CORE-009 |
| `pipeline/config.py` | 12 | 0 | Explicit validated installation configuration | — |
| `pipeline/errors.py` | 2 | 0 | Public structured domain errors | — |
| `pipeline/execution/__init__.py` | 0 | 0 | Lightweight package boundary | — |
| `pipeline/execution/admission.py` | 2 | 0 | Run admission accounting | — |
| `pipeline/execution/context_briefing.py` | 4 | 0 | Bounded current-state presentation | — |
| `pipeline/execution/coordination.py` | 5 | 3 | Read-only capacity and planning observations | CORE-002, CORE-007 |
| `pipeline/execution/coordination_memory.py` | 11 | 1 | Durable evidence-oriented legacy recovery | — |
| `pipeline/execution/intent_reconciliation.py` | 2 | 0 | Stable journal-recovery identities | — |
| `pipeline/execution/legacy_automation.py` | 1 | 0 | Saved-profile compatibility factory | — |
| `pipeline/execution/notifications.py` | 7 | 0 | Operator/control-delivery notices | — |
| `pipeline/execution/objectives.py` | 11 | 0 | Objective/planner/phase ownership | — |
| `pipeline/execution/queue_policies.py` | 5 | 0 | Category and native-reservation accounting | — |
| `pipeline/execution/refill.py` | 3 | 1 | Bounded saved-run idle reconsideration | — |
| `pipeline/execution/root_maintenance.py` | 9 | 0 | Saved root-owner evidence and recovery | — |
| `pipeline/execution/scheduler.py` | 42 | 0 | Admission, lease and physical-process lifecycle | CORE-009 |
| `pipeline/execution/worker_events.py` | 3 | 0 | Host-owned event replay and evidence provenance | — |
| `pipeline/execution/workspace_setup.py` | 4 | 0 | Read-only local checkout enrollment verification | — |
| `pipeline/graph_progress.py` | 5 | 0 | Target-specific source-bound graph claims | — |
| `pipeline/missions/__init__.py` | 0 | 0 | Lightweight package boundary | — |
| `pipeline/missions/conditions.py` | 6 | 0 | Three-valued bounded scheduling expressions | — |
| `pipeline/missions/mission_tree.py` | 13 | 1 | Delegated subtree and scope invariants | — |
| `pipeline/missions/service.py` | 21 | 0 | Transactional missions, queues, ledgers and contexts | CORE-001, CORE-009 |
| `pipeline/models.py` | 33 | 2 | Shared closed request/value contracts | CORE-008 |
| `pipeline/persistence/__init__.py` | 0 | 0 | Lightweight package boundary | — |
| `pipeline/persistence/artifacts.py` | 5 | 0 | Immutable content-addressed blob storage | — |
| `pipeline/persistence/database.py` | 7 | 0 | Explicit PostgreSQL pool/transaction/migration ownership | CORE-008 |
| `pipeline/persistence/records.py` | 14 | 0 | Shared typed relational primitives | — |
| `pipeline/persistence/schema.py` | 11 | 0 | Relational contracts and normalized indexes | CORE-008 |
| `pipeline/roadmap_index.py` | 4 | 0 | Explicit committed-source index | CORE-009 |
| `pipeline/schema_discovery.py` | 4 | 0 | Current contract lookup and corrections | — |
| **Total** | **422** | **43** | **39 files** | |

The review retains explicit transport adapters, lifecycle command branches,
transaction callbacks and schema validators. These bind concrete authority,
replay, revision and ownership rules; their distinct error paths remain useful.
The scheduler's larger methods coordinate admission and physical process
ownership across several durable records. No extraction or abstraction was
justified solely by function/file length. Compatibility code remains necessary
for persisted legacy profiles while objective runs use their separate planner
and phase lifecycle.

## Validation and limits

Foreground PostgreSQL-backed validation passed **108 tests** in 161.47 seconds:

```text
tests/test_pipeline_core_review.py
tests/test_pipeline_mission_tree.py
tests/test_pipeline_objectives.py
tests/test_pipeline_coordination.py
tests/test_pipeline_continuity.py
tests/test_pipeline_agent_planning.py
```

The run used the repository virtual environment, a disposable local PostgreSQL
database, and disk-backed development scratch through `TMPDIR`. It emitted one
existing Starlette/httpx deprecation warning. A separate read-only AST check
confirmed complete scoped file/callable coverage, all source and symbol hashes,
review dispositions and summary counts. Repository-wide validation belongs to
the integration report and is not included in this focused count.

Efficiency was assessed from control flow, query shape and explicit bounds.
Some legacy frontier/component scans remain linear in retained or project state;
per-candidate and per-limit SQL remains in admission/diagnostic paths. Objective
frontier host selection has no explicit ordering. Artifact I/O inside locked
database transactions can extend mutation-lock hold times. Snapshot indexing
uses a separate `git cat-file` invocation for each selected blob. These are
recorded scale/ordering limits, without a measured performance claim or an
assertion that every one is a verified defect.

The catalog identifies a source snapshot. Later edits require checking changed
bodies and refreshing the affected hashes. Reading these bodies does not prove
all branches executed, production remote-service integration, crash-injection
durability, mathematical statement correctness or complete system correctness.
The review did not rewrite operator databases or exercise live provider,
Forgejo or Zulip services.
