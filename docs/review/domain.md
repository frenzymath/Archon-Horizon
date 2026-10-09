# Project, operations, dashboard and migration review

The 2026-10-08 review read every complete Python function body in
`pipeline/projects`, `pipeline/operations`, `pipeline/dashboard` and
`pipeline/migrations`, including methods, validators and nested helpers. It also
read the bundled Lean performance measurement comparison script. This covers
**62 files and 259 functions**. The frozen initial migration DDL, later migration
operations, module contracts and constants were read alongside the callables.

The [source catalog](domain-catalog.json) records an individual assessment,
qualified name, source range, SHA-256 and linked findings for each function,
plus a complete file SHA-256 and module assessment. Hashes identify the reviewed
snapshot; counts and complexity metrics did not substitute for body review.

## Repairs from inspection

| Finding | Resulting behavior | Behavioral regression |
| --- | --- | --- |
| D01: YAML metadata validation | Falsy scalar/sequence front matter fails the object contract; nonfinite nested numbers fail before JSON persistence. Dates, ordinary metadata and body line endings remain supported. | `test_frontmatter_rejects_nonobjects_and_nonfinite_numbers` |
| D02: retention metadata shape | Nonobject retention markers and backup manifests remain protected and do not crash cleanup preview. Direct backup verification reports invalid metadata. | `test_malformed_retention_records_are_preserved_without_breaking_cleanup` |
| D03: health evidence contracts | An assignment must belong to the selected run even when both runs share a project. Nonfinite JSON fails before a database write. Snapshot models reject assignment scope, which their storage does not support. | Scope, JSON and snapshot tests in `test_pipeline_domain_boundaries.py` |
| D04: queue position filtering | A shared pending-queue subquery ranks the complete run before cursor or individual-session filters, keeping absolute positions in compact/full pages and session detail. | `test_queue_positions_survive_pagination_and_individual_session_filtering` |
| D05: session usage uncertainty | Summing primary-thread usage preserves any constituent uncertainty flag; unknown totals remain visibly incomplete. | `test_session_usage_keeps_the_uncertainty_of_primary_threads` |
| D06: malformed cursor identity | Nonstring UUID identities produce `invalid_cursor` instead of an unhandled `AttributeError`. | `test_malformed_cursor_id_returns_a_domain_error` |
| D07: workflow transition timing | A project with a stopping run cannot change workflow before that run settles. | `test_workflow_change_waits_for_a_stopping_run_to_settle` |
| D08: failed search retirement | Failed directory deletion releases all cleanup fences for retry. Preparation publishes readiness after cleanup and drops a loaded index on failure. | `test_failed_search_retirement_releases_cleanup_fence_and_failed_index` |

The regressions are collected in
[`tests/test_pipeline_domain_boundaries.py`](../../tests/test_pipeline_domain_boundaries.py).
No applied migration file or operator database was rewritten by these repairs.

## Purpose and retained boundaries

| Reviewed area | Assessment |
| --- | --- |
| Project bootstrap and catalog | Explicit declarative recipes, shared typed services, revision checks and normalized relations remain useful. Setup preview has no external effects; launch has a separate durable receipt. Lifecycle-owned identities do not become unrestricted catalog updates. Host limit changes account for actual unconfirmed processes and live native children. |
| Documents and evidence uploads | Safe constructors and JSON-compatible metadata are the parsing boundary. Immutable content-addressed storage and execution attribution serve evidence provenance; a blob is not an agent-completion claim. |
| Lean receipts and verification jobs | Generic library checks require source-bound successful compiler/axiom evidence and a live trusted host lease. They require no milestone manifest or baseline. Historical table names, receipt hashes and compatibility entrypoints preserve existing evidence without imposing old planning semantics on graph projects. |
| Explicit milestone workflows | Strict source contracts, route/statement review coverage and approved baselines remain isolated behind the `milestones` workflow. Long acceptance functions coordinate distinct source, authorization, citation and review invariants; splitting them solely by line count would obscure those requirements. |
| Bibliographic identity and cache | Canonical identifiers, project uniqueness and structured BibTeX avoid handwritten escaping and inconsistent identity. The disposable worker cache revalidates access remotely on each hit, uses bounded streaming, keeps revisions monotonic and handles GPFS access ordering with a logical clock. |
| Search generations | One cache owner schedules bounded source preparation; reads pin generations against eviction. Full source commits, allowlisted origins, scoped fetch credentials, process cancellation and clean-cache checks are purposeful. Size accounting is a retained-size estimate, not a benchmark or a hard guarantee about peak allocation. |
| Diagnostics, health and control plans | Read-only local diagnostics accurately distinguish file presence from online authentication. Health evidence is scoped and deduplicated; plans guard run revision/frontier and leave actual action execution to the caller's transaction. Journal housekeeping is separated from meaningful work. |
| Storage, tracing and supervision | Cleanup acts on owned, explicitly reclaimable metadata and rechecks reviewed markers. Backup export shares one PostgreSQL snapshot with its blob set. Tracing excludes raw URLs/bodies and has optional exporters. Watchdogs use explicit service names, monotonic progress, atomic failure counters and restart cooldown. |
| Workspace retention | Only managed settled assignment checkouts can be retired. Suspended sessions, unconfirmed process stops, unresolved ledger/delivery/publication state and pinned source inputs prevent deletion authorization. |
| Dashboard projections | Authorized keyset pages and batched related-record queries remain appropriate. Compact reads omit expensive provider history; detail reads preserve exact source commits, target repository context and immutable evidence. Human-readable admissions reuse the scheduler's policy. Public provider output selection excludes reasoning/user narration and applies bounded best-effort credential redaction. |
| Migrations | Historical DDL and explicit refusal of destructive downgrades preserve durable state. New additive migrations keep saved operator policies while allowing graph defaults and accepted objective phase transitions. Startup does not migrate databases implicitly. |
| Measurement comparison script | The standalone stdlib tool reads existing JSONL, checks finite arithmetic and compatible units, and preserves missing-side/zero-baseline cases. Commit/context provenance is explicitly supplied by the caller; the script executes no benchmarks and assumes no direction of improvement. |

The queue subquery is a shared abstraction justified by a concrete behavioral
defect. Existing receipt hash implementations remain separate compatibility
boundaries; this review did not alter saved evidence hashes to consolidate a
small amount of serialization code. The package initializers stay free of eager
service imports, preserving worker/client dependency isolation.

## Validation and limits

The broader project/operations/dashboard/migration suite passed **213 tests**,
with **one skipped** PostgreSQL backup/restore test requiring an explicitly
configured disposable development container. After the stopping-run guard, the
boundary and graph-planning suites passed **31 tests**. The final boundary/search
suite passed **36 tests**, validating the subsequent search cleanup repair. Root integration checks
cover the final shared checkout and distributions.

All database tests used the supplied disposable PostgreSQL database with random
isolated schemas. Commands used disk-backed development scratch and ran in the
foreground until completion. This review did not deploy, contact live provider
accounts, mutate production records or perform a complete backup restoration.

The catalog records source inspection, not branch execution or a correctness
certificate. Search allocation estimates and redaction heuristics retain their
documented limits. Filesystem permissions, abrupt host failure and external
provider schema drift remain operational failure boundaries; the inspected
cleanup retry defect now has a targeted regression.

## Installation and Git hook executable review

The final coverage sweep also read `install.sh` and `.githooks/commit-msg`
completely, including their top-level execution, comments and the installer's
three small helpers. The separate
[installation catalog](installation-catalog.json) records both exact file hashes,
the three helper bodies and nine top-level blocks. These shell files are outside
the 62-file Python count above. Tests, generated assets and upstream dependencies
are outside the executable-body audit scope.

The installer retains its explicit source download, dashboard build and
control-plane installation purpose. Its preflights, fail-fast shell settings and
owned disk-backed scratch cleanup are appropriate; it creates no database or
worker configuration. The review ran `bash -n install.sh` without executing the
installer.

The optional commit-message hook could truncate the original message after a
filtering or temporary-write failure. Following integration-owner approval, one
checked AWK transform now writes scratch beside the Git message and an atomic
same-filesystem replacement runs only after success. Exit/signal cleanup removes
unused scratch; failed helpers preserve the original and permit the commit. The
existing attribution patterns and interior blank lines are preserved. A stale
comment pointing to nonexistent CONTRIBUTING instructions was removed while the
exact opt-in hook configuration command remains documented in the hook.

`sh -n .githooks/commit-msg` passed, and
`tests/test_commit_message_hook.py` passed **six tests**. Those tests use temporary
message files, verify actual filtering and inject transform, rename and scratch
creation failures. They create no commits and change no hook configuration.
