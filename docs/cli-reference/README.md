# Command-Line Interface (CLI) Reference

The console entry point for Archon Horizon is **`horizon`**. Every command supports built-in help (`horizon <command> --help`) and machine-readable JSON output (`--json`). Command registration lives in [`cli.py`](../../src/archon_horizon/cli.py); each command is implemented under [`commands/`](../../src/archon_horizon/commands).

---

## Table of Contents

- [1. Global Options](#1-global-options)
- [2. Workspace setup & lifecycle](#2-workspace-setup--lifecycle)
- [3. Execution & orchestration](#3-execution--orchestration)
- [4. Inbox & communication](#4-inbox--communication)
- [5. Roadmap, tasks & projects](#5-roadmap-tasks--projects)
- [6. Blueprints & search](#6-blueprints--search)
- [7. Dashboard & export](#7-dashboard--export)
- [8. Environment variables](#8-environment-variables)

---

## 1. Global Options

Applied to `horizon` before the subcommand (see the root callback in [`cli.py`](../../src/archon_horizon/cli.py)):

| Option | Description |
| :--- | :--- |
| `--root <dir>` | Workspace root path (defaults to the current directory). |
| `--json` | Emit pure JSON to `stdout`; human text/banners go to `stderr`. Supported by every command. |
| `--version`, `-V` | Show the version banner and exit. |
| `--help`, `-h` | Display usage. |

### Pre-command synchronizer

Inside an agent session, every `horizon` command first prints a short digest to
`stderr` before running — unread inbox items for the current task, this session's
runtime and cumulative tokens, and any other live runs on the workspace — so an
agent notices anything it should react to. It is best-effort, only fires inside a
run/session (a human at the CLI never sees it), and only ever writes to `stderr`,
so it never pollutes `--json` stdout. Set `ARCHON_HORIZON_NO_SYNC=1` to disable
it. ([`core/synchronizer.py`](../../src/archon_horizon/core/synchronizer.py))

---

## 2. Workspace setup & lifecycle

### `horizon init`
Scaffold a new workspace, or refresh Horizon-managed files on an existing one.

| Flag | Description |
| :--- | :--- |
| `--config-json <json>` | Non-interactive config seed (e.g. `{"horizon_kind":"null"}`). |
| `--interactive` / `--no-interactive` | Prompt through setup (default) or run headless. |
| `--audit` | After scaffolding, launch the interactive post-init advisor (Horizon harness). |
| `--update` | Refresh managed artifacts (subagents, skills, MCP, git excludes) on an existing workspace; preserves your `config.yaml` and content. Non-prompting. |

### `horizon setup`
Check (and offer to install) external tools: the configured engines, the Lean 4 toolchain (`elan`/`lake`), and `gh`. ([`commands/setup.py`](../../src/archon_horizon/commands/setup.py))

### `horizon update`
Upgrade the installed `archon-horizon` package. Follow with `horizon init --update` in each workspace. ([`commands/update.py`](../../src/archon_horizon/commands/update.py))

---

## 3. Execution & orchestration

### `horizon run <target>`
Drive the Horizon loop; fresh-context helpers are dispatched by the Horizon
agent at the checkpoints described in the `horizon` skill. ([`commands/run.py`](../../src/archon_horizon/commands/run.py))

By default, `horizon run` also starts the live dashboard for the duration of the
run.

**Targets** (positional). Each target resolves in order **task id → roadmap item id → project/file**:

| Target | Effect |
| :--- | :--- |
| `<task-id>` | Run that human-created task (re-worked each round for a multi-round run). |
| `<roadmap-id>` | Infer a task from that roadmap milestone on demand and run it (the roadmap itself never auto-creates tasks). |
| `<project>` / `<file>` | Synthesize an ad-hoc task scoped to that project/file. |
| `.` | Ad-hoc task over **all** configured projects. |
| `'*'` | Run every queued task (must be the only target). |
| `horizon` | Run a **single** Horizon prover session over the current focus. |

**Flags:**

| Flag | Description |
| :--- | :--- |
| `--task <id>` | Pin one task (alternative to a positional target). |
| `--rounds <n>` | Override the configured round count. |
| `--dry-run` | Plan only; print what would run, don't invoke Horizon. |
| `--resume <id\|latest>` | Resume an interrupted run from its last unfinished round. |
| `--backend <default\|interactive>` | `default` streams a headless transcript (orchestrated). `interactive` hands the terminal to the Horizon engine so you can type prompts. |
| `--host <host>` / `--port <port>` | Dashboard bind address and port for the run. |
| `--public` | Bind the run dashboard to `0.0.0.0` for remote VMs, containers, or port-forwarded sessions. |
| `--no-dashboard` | Do not start the live dashboard; useful for scripts and headless runs. |

### `horizon discuss`
Open an **interactive** session with the workspace. The agent reads the docs and on-disk state, summarizes status and recent runs, and can manage projects/tasks/inbox/roadmap — but only modifies things when you explicitly ask. ([`commands/discuss.py`](../../src/archon_horizon/commands/discuss.py))

### `horizon sync`
Sync inbox providers — e.g. import GitHub issue/PR shadows into the local inbox via the `gh` CLI (see the `github:` config). ([`commands/sync.py`](../../src/archon_horizon/commands/sync.py))

### `horizon permissions`
Read the workspace's standing delegation consent — what a running agent is allowed to do on the user's behalf before it delegates work it cannot do inline. The **default is deny**: an agent may not create tasks or launch runs. Reports `allow_launch_tasks`, `allow_launch_runs`, `max_parallel_sessions`, and declared `accounts`, plus any free-form notes (e.g. api limit-reset times, which config dir to use) the human records under `workspace.delegation` in `config.yaml`. Reading this authorizes nothing on its own. ([`commands/permissions.py`](../../src/archon_horizon/commands/permissions.py))

| Flag | Description |
| :--- | :--- |
| `--json` | Emit the delegation config as JSON to `stdout`. |

### `horizon attempt`

Preserve a substantial rejected or paused draft outside the working tree so a
later session can inspect the dead end without treating it as a durable commit.
Artifacts live under the active session's `attempts/` directory.
([`commands/attempt.py`](../../src/archon_horizon/commands/attempt.py))

- `horizon attempt save <files...> --reason <text> [--diagnostics <file>] [--json]`
- `horizon attempt list [--json]`

### `horizon check`

Run `lake build` or one standalone Lean file while holding a workspace-wide
check lock. Identical concurrent successful requests are reused after waiting;
results and timeouts are recorded under the active session's `checks/`
directory. ([`commands/check.py`](../../src/archon_horizon/commands/check.py))

- `horizon check [targets...] [--timeout <seconds>] [--json]`
- `horizon check --lean <file> [--timeout <seconds>] [--json]`

---

## 4. Inbox & communication

Implemented in [`commands/inbox.py`](../../src/archon_horizon/commands/inbox.py).

- `horizon inbox list [--status open|closed|archived] [--kind K] [--label L] [--project P] [--to R] [--task <id>] [--mine] [--unread] [--query TEXT] [--limit N] [--comments N]` — show inbox items, with optional filters for triage and capped comment output. `--task` shows a task's inbox (items owned by it PLUS shared/unowned items); `--mine` is that, resolved from the session; `--unread` keeps only items you (this task/run/human) have not marked read.
- `horizon inbox add --kind <k> --body <text> [--project P] [--to R] [--owner <task>] [--mine] [--author A] [--agent A] [--persistent|--temporary] [--pending]` — add a hint/issue. `--to` addresses the recipient it is FOR: `horizon | human | project:<name> | task:<id> | run:<id>` (a `task:`/`run:` recipient is a direct message reaching only that recipient). `--owner <task>` files the item into a task's inbox; `--mine` owns it to my task from the session.
- `horizon inbox comment <id> --body <text> [--author A]` — post a comment.
- `horizon inbox read <id> [--reader R]` · `unread <id> [--reader R]` — mark an item read / unread for a reader (defaults to my task / run / human); read-state is per-reader, so shared items track who has read them.
- `horizon inbox own <id> [--owner <task> | --mine | --shared]` — move an item into a task's inbox, or `--shared` to clear ownership (visible to everyone).
- `horizon inbox protect [--file F] [--declaration D] [--blueprint-node N] [--project P] --body <why>` — add a semantic standing protection.
- `horizon freeze add <agent|project|file|declaration|blueprint-node> <pattern>` — add an enforced config-backed freeze.
- `horizon freeze list` / `horizon freeze remove <kind> <pattern>` — inspect or remove enforced freezes.
- `horizon inbox edit <id> [--body B] [--kind K]` · `edit-comment <id> --index N --body B`.
- `horizon inbox label <id> <labels>` · `complete <id>` · `archive <id>` · `reject <id>` · `delete <id>`.

---

## 5. Roadmap, tasks & projects

**Roadmap** — the project's **mathematical status**: the main theorems and infrastructure formalized and still to build. It is a map that guides the work, **not** a task queue — marking an item active does not launch anything. Ground keeps it current; a human launches a milestone with `horizon run <roadmap-id>`, which infers a task from it. ([`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py)):
`list [--focus --max-depth --milestone --owner]` · `add --id --title --project [--summary|--summary-file --status --kind --priority --parent --depth --owner --milestone]` · `set <id> [--status --summary|--summary-file --title --priority --kind --parent --depth --owner --milestone --pin-commit --unpin-commit …]` · `comment <id> --body` · `remove <id>`.

`--owner` records the responsible team/agent and `--milestone` a grouping label (both filterable on `list`); on `set`, `--pin-commit`/`--unpin-commit` (repeatable) attach or drop commit SHAs pinned to the item as deliverables.

**Tasks** — the human's lever for launching sessions. Human-authored only; agents may read and `comment` (to suggest an edit) but `add`/`set`/`remove` refuse an agent (`ARCHON_HORIZON_AGENT_ROLE` set) — to propose work an agent opens an inbox item for the human. Also inferred on demand from a roadmap id. ([`commands/task.py`](../../src/archon_horizon/commands/task.py)):
`list` · `show <id>` · `add --id --project --objective [--title --projects --file --priority --status]` · `set <id> [--status --priority --objective --title --roadmap-ref --inbox-ref]` · `comment <id> --body` · `remove <id>`.

On `set`, `--roadmap-ref`/`--inbox-ref` (each repeatable) link the task to roadmap/inbox item id(s), replacing any existing refs; a status change then propagates through linked roadmap items.

**Projects** ([`commands/project.py`](../../src/archon_horizon/commands/project.py)):
`add <name> <path> [--type --build]` · `archive <name>` · `remove <name>` · `merge <dest> <source>`.

**Skills** ([`commands/skills.py`](../../src/archon_horizon/commands/skills.py)): `list` · `install`.

**Ledger** ([`commands/ledger.py`](../../src/archon_horizon/commands/ledger.py)) — the out-of-tree agent source journal (`.archon-horizon/vcs/workspace.git`). It records Lean, blueprints, and `config.yaml` only; Horizon state and `**/hgraph/` stay on disk.

- `horizon ledger status [--json]` — count tracked paths that current policy treats as non-source (state / hgraph).
- `horizon ledger prune [--dry-run] [--gc] [--json]` — drop those paths from the ledger index in one commit (working tree untouched). `--gc` runs `git gc --prune=now` afterward to reclaim pack space from old blobs. Use on existing workspaces that still track pre-policy state/hgraph noise.

---

## 6. Blueprints & search

- `horizon blueprint [--json]` — parse LaTeX blueprints and write JSON DAGs to `.archon-horizon/blueprints/`. ([`commands/blueprint.py`](../../src/archon_horizon/commands/blueprint.py))
- `horizon graph [-p PROJECT] <operation> [...]` — use the vendored semantic graph. Operations include `sync`, `stats`, `list`, `get`, `frontier`, `ancestors`, `descendants`, `view`, `add`, `modify`, and `delete`. ([`commands/graph.py`](../../src/archon_horizon/commands/graph.py))
- `horizon search <query> [--name P] [--type SIG] [--lib L] [--limit N] [--reindex]` — offline BM25 / name / signature search over Mathlib and local `.lean` sources. ([`commands/search.py`](../../src/archon_horizon/commands/search.py))

---

## 7. Dashboard & export

[`commands/dashboard.py`](../../src/archon_horizon/commands/dashboard.py)

- `horizon dashboard [--host --port --public --dist]` — live web server (default `127.0.0.1:8765`; `--public` binds `0.0.0.0` for remote/container/port-forwarded use).
- `horizon dashboard --static [--out <dir>] [--dist frontend/dist] [--workflow]` — export a self-contained static snapshot (for GitHub Pages), optionally writing a Pages deploy workflow.

> The static export includes the **run logs** (transcripts + reports), but the logs viewer only renders when a **built SPA** is supplied (`--dist frontend/dist`). Without it the export falls back to a read-only page that omits logs. See [Dashboard & Search](../dashboard-and-search/README.md).

---

## 8. Environment variables

| Variable | Purpose |
| :--- | :--- |
| `IS_SANDBOX=1` | Allow Claude Code / containerized agents to run under root. |
| `ARCHON_HORIZON_AGENT_ROLE` | Per-session role (`horizon`; older logs may contain `ground`); enforces agent write-lanes (e.g. agents can't author tasks). |
| `ARCHON_HORIZON_ALLOW_SECRETS=1` | Bypass the autogit pre-commit secret guard. |
| `ARCHON_HORIZON_NO_SYNC=1` | Disable the pre-command synchronizer digest (see §1). |
| `CLAUDE_CONFIG_DIR` / `CODEX_HOME` | Per-harness engine config/auth home (see the harness `options.config_dir`). |
