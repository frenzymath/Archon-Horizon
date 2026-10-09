# Worker and agent-client function review

This independent source review read every function body and module contract in
`pipeline/worker/**/*.py`, `pipeline/worker_config.py` and `pipeline/client.py`.
It includes nested functions, the generic Lean/library verifier and compatibility
entrypoints. Exact symbol coverage, source lines and file hashes are recorded in
[worker-catalog.json](worker-catalog.json). Search and instruction resource
modules were assigned to another reviewer.

## Findings and repairs

| Finding | Behavior and resolution |
| --- | --- |
| WRK-001 / build diagnostics | `_command` created a second stderr pipe but did not drain it in merged-diagnostic mode. A verbose compiler could block until its deadline. Stderr now shares stdout's pipe in that mode. A native subprocess writing 1 MiB to stderr completes with bounded retained diagnostics. |
| WRK-002 / source-bound library receipts | `verify_library` attributed the final audit to the initially observed commit without a final cleanliness/head check. It now rejects dirty source or a changed head after auditing. Regressions cover both an uncommitted edit and a concurrent commit. |
| WRK-003 / compatibility comparator | The extracted legacy verifier called `check` without importing it. Its shared build-check import is restored; a comparator-path regression reaches the build helper and reports unavailable tools normally. |
| WRK-004 / physical verification fencing | Stopping only the helper's process group missed compiler groups created inside its session. The verifier captures PID/start identity and boot identity at launch, then uses the existing session-wide process fence. Tests cover living and already-exited leaders with separate compiler groups. Unknown physical stop propagates, stops the worker and prevents a finish receipt or another check claim. |
| WRK-005 / durable preparation evidence | The worker transport treated `workspace_prepared` as durable evidence, but the local journal's age filter treated it as expiring telemetry. The age filter now preserves that receipt like terminal/publication evidence. A replay-horizon regression verifies it remains deliverable. |
| WRK-006 / client outage backoff | After its bounded inline transport retries, the agent client left the durable intent's attempt count and retry time unchanged. A new CLI invocation could immediately repeat it during an outage. Transport failures now persist the same backoff policy used for retryable server responses, retain the same key/body and propagate the original transport exception. A restarted-client regression confirms it cannot immediately resend. |
| WRK-007 / abrupt daemon death | Verification-job helper ownership is not journaled before launch. An abrupt daemon kill can leave a helper alive while a future daemon reclaims the job. This remains a limitation: helpers/builds have finite deadlines, and compiler admission uses shared host slots, but a new daemon cannot prove that older helper has physically stopped before starting a new verification. Provider execution recovery has a stronger persisted ownership protocol. |

WRK-007 is recorded explicitly; this review does not claim complete recovery
under arbitrary daemon/process death. A durable verifier ownership protocol
would need atomic launch identity, startup recovery, lease fencing and retention
rules together. Adding only a PID file would not establish that protocol.

## Module and function contracts reviewed

| Module | Review focus and result |
| --- | --- |
| `worker/__init__.py` | Import-only package boundary; opens no credentials, database or worker state. |
| `activity_replay.py` | Bounded stdout replay, stable event identities, cursor commits after enqueue and partial-line handling. Adds display telemetry without provider execution or usage replay. |
| `build_engine.py` | Source fingerprints, resource lock ordering, compiler/preparation lanes, storage floors, subprocess output bounds and cancellation. Existing Git dependency checkouts remain untouched; reflink donors must match pinned bytes. WRK-001 repaired. |
| `cache_retention.py` | Exclusive eviction versus shared cache leases, inode/mtime checks, bounded oldest-entry selection and conservative file scope. Git object pools, tracked sources and recent native artifacts remain protected. Allocation/mtime observations can lag on network filesystems. |
| `codex_lifecycle.py` | Scoped rollout matching, bounded native call correlation, stable journal identities, cursor durability and cumulative counter scope. No prompts/answers leave the reader. |
| `contracts.py` | Closed operation kinds, canonical finite JSON, bounded identifiers, explicit tool-path environment and positive execution grants. Dataclasses are internal runtime contracts; server/operator models provide stricter external validation. |
| `daemon.py` | All harness validation, startup claims, lease renewal, workspace exclusion, recovery, publication delivery and independent service lanes. Persisted native context is resumed; uncertain physical stops retain recovery ownership. Legacy orchestrator handling remains compatibility behavior, outside new objective runs. |
| `git_recovery.py` | Separate trusted Git index/config, staged/dirty recovery identities, deterministic operation keys, authenticated remote restrictions and confirmation of immutable recovery refs. Recovery never merges a project branch. |
| `journal.py` | Transactional delivery claims, epoch/boot/monotonic lease fencing, reserved acknowledgement capacity, diagnostics eligibility and pending-intent preservation. WRK-005 repaired. |
| `lean_build.py` | Operator build policy validation, finite deadlines, current-source snapshots, cancellation and deferred outcomes. Compilation concurrency is independent of agent slots. |
| `lean_verify.py` | Git-derived complete changed-module scope, transitive axiom audits, exact committed inputs and policy-independent library verification. No milestone planning metadata is required. WRK-002 repaired. |
| `milestone_jobs.py` | Import-only compatibility alias to the generic verification lane; does not add a second implementation. |
| `milestone_verify.py` | Explicit legacy milestone identity/table/contract/comparator verification; current graph workflows do not invoke these planning gates. WRK-003 repaired. |
| `provider.py` | Native harness arguments and permissions, bounded structured streams, pre-execution PID checkpoint gate, request uncertainty and full owned-session process fencing. Process fencing is lifecycle control, not isolation against deliberate `setsid` escapes. |
| `resource_health.py` | Linux host/cgroup limits and ancestor limits, PSI observations and admission reasons. Unknown readings remain unknown; this is admission guidance, not a memory reservation or OOM guarantee. |
| `sandbox.py` | Rootless image digest, resource policy, mount overlap/protected roots, environment filtering and network semantics. Outbound networking is not a destination allowlist. |
| `skills.py` | Bounded content-addressed instruction bundles, path/encoding/digest validation, immutable materialization and verification before reuse. |
| `storage_guard.py` | Read-only root inspection, byte floors and separately reported cleanup targets. It does not delete files or assume a percentage is an admission quota. |
| `transport.py` | One bounded worker HTTP attempt, current auth, bounded artifact reads and durable retry claims. Publication/terminal/preparation evidence is retained independently of telemetry retry budgets. |
| `verification_jobs.py` | Isolated exact-source checkouts, generic-library versus explicitly enabled legacy dispatch, claim-token heartbeat/finish fencing and process settlement. WRK-004 repaired; WRK-007 remains. |
| `workspace_retention.py` | Only generated settled assignment paths may be removed; live ownership is checked by the API, and actual Git publication/cleanliness is checked by the host. Ignored notes, unknown cache contents and edited dependency trees prevent deletion. |
| `workspaces.py` | Durable preparation intent, assignment-bound path/branch/commit, gated subprocess identity, lease checks and crash recovery around staged worktree rename. A previously admitted dirty workspace is preserved. |
| `worker_config.py` | Closed explicit configuration, protected-root overlap, private credential reads and worker-only imports. Generic `lean_checks` requires a managed unrestricted build host; `milestone_checks` explicitly permits compatibility jobs. |
| `client.py` | Durable request identity, current credentials, known no-effect rejection classification, concurrent journal migration, bounded notices/replay and durable repair obligations. WRK-006 repaired. |

The daemon remains large because it binds execution leases, native requests,
workspace ownership and publication settlement. Its function bodies were read
individually. A future extraction should preserve those transaction/lifecycle
boundaries, especially the distinction between an agent stopping and the host
confirming its processes have stopped. This review avoided turning each
conditional into a new policy abstraction.

## Validation

`tests/test_pipeline_worker_review.py` passed **10 regressions**, including real
subprocess output/process-group behavior. The scoped existing worker, build,
Lean-policy, client-concurrency and agent-contract run passed **167 tests**;
30 environment-dependent cases were skipped in that invocation. The final
repository validation report records the broader run with its disposable
PostgreSQL/Lean environment. All fixtures use dedicated development scratch and
synthetic services. No operator journal, live workspace or production database
was opened. The scoped diff whitespace check passed.

These tests establish the repaired paths and integration contracts. They do not
constitute a live Codex/Claude rollout, a rootless-container deployment test or a
resource guarantee for a large Lean project.
