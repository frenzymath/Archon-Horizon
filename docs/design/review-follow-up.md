# v0.2.0 Manual Review Implementation

This work implements the operator's notes in the current beta-development
worktree. It preserves operator state and earlier working changes; no production
database or live formalization run was migrated. The package still declares
alpha status. Passing these checks does not declare a beta or stable release.

## Review Notes And Result

| Note | Implemented result and evidence |
| --- | --- |
| AGENTS.md / CLAUDE.md | Kept and explained in [Contributing](../../CONTRIBUTING.md). They guide development of Horizon; dispatched projects use pinned API catalogs. CLAUDE imports the shared source. |
| .gitignore | Grouped protections, removed redundant frontend build and blanket lockfile rules, protected private environment variants and retained reusable examples and tracked dependency locks. |
| Migrations | Kept required Alembic history. Startup checks the revision; explicit migration supports fresh PostgreSQL installations and upgrades. Added revision 0029 rather than rewriting applied history. |
| Horizon skill introduction | Identifies Horizon, links its repository and explains consulting a matching local checkout. PATH locates tools, not source. |
| Documentation site | Added MkDocs, strict build, navigation, root-document inclusion, source links and an artifact-building Pages workflow. Publication is a separate manual workflow action after source review. |
| Agent-assisted configuration | README recommends working with a coding agent and reviewing the generated installation plan. |
| Dashboard references | Added project catalog/search/pagination, selected detail, source links, BibTeX display/download, permission-aware registration/editing, conflict handling and durable uncertain-write retries. See [References](../references.md). |
| README organization | Installation, security/sandbox, resource parallelism and state paths now have explicit sections. |
| Dashboard demo | Added a generated synthetic fixture and separate build of the actual dashboard, with read-only transport and repository-subpath browser checks. See [demo](../dashboard-demo.md). |
| Additional tools | Added an AST review inventory with documentation, branches/nesting, optional coverage and explicit review-ledger inputs. Statistics select review work; they do not establish correctness. |
| Version tags | Created [v0.2.0-alpha.1](https://github.com/frenzymath/Archon-Horizon/tree/v0.2.0-alpha.1) for the already published alpha commit 0df5571. It excludes later development work. [Release guidance](../releases.md) explains pinning and future tags. |
| Context briefing | Added [Agent context](../agent-context.md), distinguishing bounded read-only database views, initial prompts and native history. |
| Independent code review | Separate readers reviewed complete source bodies and recorded exact catalogs, findings and tests. The [implementation specification](implementation-audit.md) was derived from executable code without design prose as evidence. See coverage below. |
| Hardcoded prompts | Moved behavioral instructions into [Markdown assets](../../src/archon_horizon/pipeline/instructions/templates). New immutable bundles pin their text/digests; the instruction browser displays them. IDs, serialization, authorization and status/error text remain code. |
| ORCHESTRATOR | Fresh orchestrated launches and new orchestrator assignments are rejected. Saved legacy profiles remain isolated in execution/legacy_automation.py; planners and maintainers own diagnosis in new runs. |
| Milestone-specific execution | New projects default to agent-led graph planning. Milestones are labeled ordinary nodes. Maintainer phase decisions need source evidence, not a mandatory frozen/compiler-certified milestone baseline. Strict existing projects retain explicit compatibility policy. |

## Agent-Led Planning And Lean Verification

Roadmap PRs remain the accepted source for objectives, graph routes, milestones
and their amendments. Ordinary source paths, dependency DAG validity, revision
conflicts, permissions and review provenance remain checked. An idle existing
strict project can be switched explicitly to graph workflow by a human; active
run changes are rejected. Source edits and migration do not silently reinterpret
accepted legacy contracts.

Objective formalization can start directly from its objective. Phase acceptance
records the maintainer's evidence and chosen next inputs; revision 0029 allows a
transition only when it matches that recorded acceptance. This also repairs an
older immutable-field trigger that blocked objective phase advancement.

Generic library audits now use `worker/lean_verify.py`, `projects/lean_checks.py`
and `projects/verification_jobs.py`, with `/api/v3/lean/verifications` and
`/api/v3/lean/checks`. They require an exact-source ready library checkout and
host lease, independent of milestone manifests. Historical storage names, routes
and flags retain old receipt/lease identities. Audits reject admissions in
changed declarations and their transitive dependencies; semantic correctness
and merge authority remain separate review decisions.

## Prompt And Context Provenance

The Markdown renderer substitutes allowlisted tokens once. JSON braces, shell
variables and inserted user content remain literal. New sessions receive all
templates and hashes in their immutable catalog. Retained sessions use those
pinned templates; missing or corrupted new-bundle entries fail explicitly.
Historical catalogs retain their original pinned core/function fields, with an
explicit compatibility fallback for fragments that older releases never pinned.
Prepared reviewer manifests keep their rendered text and source identities.

Generated mission and recovery instructions are rendered at record creation and
stored with their owner; editing installed assets does not rewrite those
persisted instructions. Objective continuations avoid repeating full role
contracts, and ledger excerpts enforce the aggregate character budget including
markup and identifiers. Context views continue to expose links for omitted detail.

## Independent Review Evidence

| Scope | Evidence |
| --- | --- |
| Core entrypoints, API, scheduler, missions and persistence | [Core review](../review/core.md), [catalog](../review/core-catalog.json) |
| Project/domain services, operations, dashboard projections and migrations | [Domain review](../review/domain.md), [catalog](../review/domain-catalog.json) |
| Instructions, reviews, integrations, providers and search | [Backend review](../review/backend.md), [catalog](../review/backend-catalog.json), [prompt review](../review/prompts.md) |
| Workers, configuration and agent client | [Worker review](../review/worker.md), [catalog](../review/worker-catalog.json) |
| React sources and Vite configuration | [Frontend review](../review/frontend.md), [catalog](../review/frontend-catalog.json), [References](../review/references-ui.md) |
| Installer and commit hook | [Installation catalog](../review/installation-catalog.json); hook failures preserve the original message and scratch is cleaned |
| Developer scripts, documentation and demo | [Tooling/demo review](../review/docs-demo.md), [catalog](../review/tooling-catalog.json) |

The [coverage reconciliation](../review/source-coverage.json) matches all **219
source files** against their current hashes: 166 Python files with 1,210 named
functions, 51 TypeScript/configuration files with 1,130 function bodies, and two
shell entrypoints. Tests, generated builds and upstream dependencies are outside
this source-body review. The independent core reader produced the specification
before reading design prose; other readers reviewed comments as part of normal
code assessment.

Catalog entries record actual body inspection at a source hash. They are not
proofs or replacements for runtime tests. Changed source requires a fresh review
of the changed body. Large lifecycle modules were inspected individually; they
were not mechanically split merely to reduce branch counts or file sizes.

## Deployment Status And Limits

The site and demo build locally and as reviewable CI artifacts. The repository's
existing public Pages site is an older dashboard snapshot; this task does not
replace it with uncommitted source. Publish the reviewed Documentation workflow
on main to deploy the new site. No production service, database, provider
configuration or live workspace was changed.

The catalogs document remaining limits, including abrupt verification-daemon
death before durable helper ownership, connector schema drift, lexical Lean
presentation and semantic mission paraphrases. Live-provider sessions,
large-project resource behavior and rootless deployments require disposable
operator trials before release. See the [validation record](../review/validation.md)
for the completed integration checks and exact skipped scope.
