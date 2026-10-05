# Contributing to Archon Horizon

Read [AGENTS.md](AGENTS.md) and [architecture](docs/architecture.md). The runtime
is `src/archon_horizon/pipeline`; all CLI entrypoints invoke that implementation.

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
| `src/archon_horizon/pipeline/` | API, domain services, PostgreSQL, scheduler, integrations |
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
