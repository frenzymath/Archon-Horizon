# Contributing to Archon Horizon

Read [AGENTS.md](AGENTS.md) and [architecture](docs/architecture.md). The runtime
is `src/archon_horizon/pipeline`; all CLI entrypoints invoke that implementation.

`AGENTS.md` governs agents developing Horizon's source. `CLAUDE.md` imports it
so both development harnesses share one maintained instruction source. These
files do not configure dispatched formalization runs: those use the API's pinned
skills, prompts and descriptors. Keep development rules out of project missions.

## Setup And Checks

Use Python 3.11 or newer and Node.js 20:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
npm --prefix src/archon_horizon/frontend ci
mkdir -p "$HOME/.horizon/development-tmp"
export TMPDIR="$HOME/.horizon/development-tmp"
python -B -m pytest -q
python scripts/version.py --check
npm --prefix src/archon_horizon/frontend run typecheck
npm --prefix src/archon_horizon/frontend test
npm --prefix src/archon_horizon/frontend run build
npm --prefix src/archon_horizon/frontend run test:pipeline
```

Run checks in the foreground and wait for completion. Install the browser once
with `npx playwright install chromium` in the frontend directory. Browser fixtures
use isolated data; inspect a fixture before pointing it at a real service.

Set `HORIZON_PIPELINE_TEST_URL` to a disposable PostgreSQL database for database
and HTTP tests. They create/drop random schemas. Never use the operator database.
CI provides PostgreSQL 17. Backup tests additionally require
`HORIZON_PIPELINE_BACKUP_TEST_CONTAINER` naming a disposable container labelled
`archon-horizon.scope=pipeline-development`.

## Layout

| Path | Responsibility |
| --- | --- |
| `src/archon_horizon/pipeline/cli.py` | Operator and dispatched-agent commands |
| `src/archon_horizon/pipeline/` | Stable entrypoints, configuration, authentication, shared request contracts |
| `src/archon_horizon/pipeline/review/` | Review contracts, packets, invocations, identities and decisions |
| `src/archon_horizon/pipeline/integrations/` | Forgejo/Zulip transport, browser identities and delivery reconciliation |
| `src/archon_horizon/pipeline/missions/` | Mission trees, conditions and transactional domain services |
| `src/archon_horizon/pipeline/execution/` | Scheduling, admission, execution events and coordination recovery |
| `src/archon_horizon/pipeline/dashboard/` | Scoped read models and activity presentation |
| `src/archon_horizon/pipeline/instructions/` | Catalog discovery, prompt construction and pinned bundles |
| `src/archon_horizon/pipeline/persistence/` | PostgreSQL schema, connections, record primitives and blob storage |
| `src/archon_horizon/pipeline/projects/` | Project setup, source documents, milestones, references and search |
| `src/archon_horizon/pipeline/operations/` | Installation diagnostics, health, storage and supervision |
| `src/archon_horizon/pipeline/providers/` | Native event parsing, observations and usage accounting |
| `src/archon_horizon/pipeline/worker/` | Provider execution, local journals, publication, sandbox and builds |
| `src/archon_horizon/pipeline/skills/` | Bundled skills grouped into operations, Lean, and review procedures |
| `src/archon_horizon/pipeline/subagents/` | Implementation, research, validation, planning and reviewer descriptors |
| `src/archon_horizon/pipeline/migrations/` | Required Alembic schema history for current databases |
| `src/archon_horizon/search/` | Lean index and standalone workspace search MCP |
| `src/archon_horizon/frontend/` | Current React dashboard and tests |
| `deploy/pipeline/` | Service and explicit configuration examples |
| `scripts/` | Version and distribution validation |

Preserve operator data and unrelated working changes. Keep credentials, provider
homes, journals, databases, caches, and release artifacts outside source control.

## Code Documentation

Write docstrings for modules and public interfaces where readers need to know
their purpose, contracts, side effects, or limitations. Add nearby comments for
choices that the implementation alone does not explain:

- For constants, state their units, scope, and meaning. Distinguish compatibility
  revisions from tuning defaults and hard limits; explain when a revision must
  change. Label heuristics as such instead of implying measured optimal values.
- Explain security boundaries and failure behavior, such as why a credential is
  scoped, a path is rejected, or a cache miss is recoverable.
- Record ordering, locking, transaction, and retry invariants where changing them
  could break correctness. Link to the authoritative policy or helper when useful.
- Describe current behavior and known limitations. Avoid invented historical
  reasons, unsupported security guarantees, and comments that restate each line.

Keep comments and docstrings current when changing the behavior they describe.
JSON examples must remain valid JSON; explain their choices in the configuration
models or setup guide rather than inserting comments or unsupported fields.

## Changes

Use typed request contracts and revision-checked domain services. Keep queue
claims and state changes transactional. Retries must reconcile the same intent;
an uncertain network outcome is not proof an operation failed. API startup checks
the database revision; apply schema changes only through the explicit `migrate`
command. Never rewrite applied migration history.

Workers must remain usable with `[pipeline-worker]` alone. Do not import server
or search dependencies in worker startup. Use structured provider streams and
retain unknown usage as unknown. Keep browser reads cancellable, bounded and
cached, and preserve drafts on refresh or revision conflicts.

Group implementation modules by responsibility; the number of files is a
readability signal, not a package size limit. Keep package `__init__.py` files
free of eager service imports. Import concrete modules rather than re-exporting
an entire subsystem. Resolve packaged assets through `pipeline._resources`
instead of assuming they are beside the consuming module. The documented
CLI/API entrypoints and applied migration history remain stable when internals
are reorganized. Check source imports, worker dependency isolation and built
distribution contents after moving modules.

Skills use `metadata.category` (`operations`, `lean`, `review`, or `custom`).
Bundles expose grouped `SKILLS.md` and `SUBAGENTS.md` indexes; agents read
relevant bodies on demand. Specialist descriptors include skill references and
live under their category in `pipeline/subagents/`; their names do not create
execution roles, functions or provider-native agent types.
`skill_source_root` supplies operator additions. Reviewer source descriptions
live under `pipeline/subagents/reviewers/`; the same loader
feeds presets and prompts. Existing project descriptors are revisioned records,
edited in the dashboard or API. Source edits do not overwrite them or an existing
session's pinned bundle. See [reviewer workflow](docs/pipeline-reviewers.md).

Build the frontend before producing wheels/source archives, then run
`scripts/check_wheel.py` and `scripts/check_sdist.py`. Use a clean build directory
so removed source files cannot survive as stale package artifacts.

## Documentation And Review Tools

The [documentation site guide](docs/documentation-site.md) covers the MkDocs
build, Pages workflow, generated dashboard demo and local preview. Keep synthetic
demo records separate from installation data. `build:demo` produces a standalone
artifact without changing the dashboard included in wheels.

Use `scripts/review_inventory.py` to enumerate Python files and scoped functions,
their documentation, branch/nesting counts and optional line-coverage or explicit
review evidence. Use the inventory to choose and record review work; metrics do
not establish correctness or justify refactors by themselves.
