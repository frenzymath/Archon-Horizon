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

---

## 2. Workspace setup & lifecycle

### `horizon init`
Scaffold a new workspace, or refresh Horizon-managed files on an existing one.

| Flag | Description |
| :--- | :--- |
| `--config-json <json>` | Non-interactive config seed (e.g. `{"ground_kind":"null"}`). |
| `--interactive` / `--no-interactive` | Prompt through setup (default) or run headless. |
| `--audit` | After scaffolding, launch the interactive post-init advisor (Ground harness). |
| `--update` | Refresh managed artifacts (subagents, skills, MCP, git excludes) on an existing workspace; preserves your `config.yaml` and content. Non-prompting. |

### `horizon setup`
Check (and offer to install) external tools: the configured engines, the Lean 4 toolchain (`elan`/`lake`), and `gh`. ([`commands/setup.py`](../../src/archon_horizon/commands/setup.py))

### `horizon update`
Upgrade the installed `archon-horizon` package. Follow with `horizon init --update` in each workspace. ([`commands/update.py`](../../src/archon_horizon/commands/update.py))

---

## 3. Execution & orchestration

### `horizon run <target>`
Drive a Ground/Horizon collaboration run. ([`commands/run.py`](../../src/archon_horizon/commands/run.py))

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
| `ground` | Run a **single** Ground planning session (no alternation). |
| `horizon` | Run a **single** Horizon prover session over the current focus. |

**Flags:**

| Flag | Description |
| :--- | :--- |
| `--task <id>` | Pin one task (alternative to a positional target). |
| `--rounds <n>` | Override the configured round count. |
| `--dry-run` | Plan only; print what would run, don't invoke Horizon. |
| `--resume <id\|latest>` | Resume an interrupted run from its last unfinished round. |
| `--backend <default\|interactive>` | `default` streams a headless transcript (orchestrated). `interactive` hands the terminal to the engine for a single role (`ground`/`horizon`) so you can type prompts. |
| `--host <host>` / `--port <port>` | Dashboard bind address and port for the run. |
| `--public` | Bind the run dashboard to `0.0.0.0` for remote VMs, containers, or port-forwarded sessions. |
| `--no-dashboard` | Do not start the live dashboard; useful for scripts and headless runs. |

### `horizon discuss`
Open an **interactive** session with the workspace (using the Ground harness). The agent reads the docs and on-disk state, summarizes status and recent runs, and can manage projects/tasks/inbox/roadmap — but only modifies things when you explicitly ask. ([`commands/discuss.py`](../../src/archon_horizon/commands/discuss.py))

### `horizon sync`
Sync inbox providers — e.g. import GitHub issue/PR shadows into the local inbox via the `gh` CLI (see the `github:` config). ([`commands/sync.py`](../../src/archon_horizon/commands/sync.py))

---

## 4. Inbox & communication

Implemented in [`commands/inbox.py`](../../src/archon_horizon/commands/inbox.py).

- `horizon inbox list [--status open|closed] [--kind K] [--label L] [--project P] [--to R] [--query TEXT] [--limit N] [--comments N]` — show inbox items, with optional filters for triage and capped comment output.
- `horizon inbox add --kind <k> --body <text> [--project P] [--to ROLE] [--author A] [--persistent|--temporary] [--pending]` — add a hint/issue.
- `horizon inbox comment <id> --body <text> [--author A]` — post a comment.
- `horizon inbox protect [--file F] [--declaration D] [--project P] --body <why>` — add a standing soft-freeze protection.
- `horizon inbox edit <id> [--body B] [--kind K]` · `edit-comment <id> --index N --body B`.
- `horizon inbox label <id> <labels>` · `complete <id>` · `reject <id>` · `delete <id>`.

---

## 5. Roadmap, tasks & projects

**Roadmap** — the project's **mathematical status**: the main theorems and infrastructure formalized and still to build. It is a map that guides the work, **not** a task queue — marking an item active does not launch anything. Ground keeps it current; a human launches a milestone with `horizon run <roadmap-id>`, which infers a task from it. ([`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py)):
`list` · `add --id --title --project [--summary|--summary-file --status --kind --priority]` · `set <id> [--status …]` · `comment <id> --body` · `remove <id>`.

**Tasks** — the human's lever for launching sessions. Human-authored only; agents may read and `comment` (to suggest an edit) but `add`/`set`/`remove` refuse an agent (`ARCHON_HORIZON_AGENT_ROLE` set) — to propose work an agent opens an inbox item for the human. Also inferred on demand from a roadmap id. ([`commands/task.py`](../../src/archon_horizon/commands/task.py)):
`list` · `show <id>` · `add --id --project --objective [--title --projects --file --priority --status]` · `set <id> [--status --priority --objective --title]` · `comment <id> --body` · `remove <id>`.

**Projects** ([`commands/project.py`](../../src/archon_horizon/commands/project.py)):
`add <name> <path> [--type --build]` · `archive <name>` · `remove <name>` · `merge <dest> <source>`.

**Skills** ([`commands/skills.py`](../../src/archon_horizon/commands/skills.py)): `list` · `install`.

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
| `ARCHON_HORIZON_AGENT_ROLE` | Per-session role (`ground`/`horizon`); enforces agent write-lanes (e.g. agents can't author tasks). |
| `ARCHON_HORIZON_ALLOW_SECRETS=1` | Bypass the autogit pre-commit secret guard. |
| `CLAUDE_CONFIG_DIR` / `CODEX_HOME` | Per-harness engine config/auth home (see the harness `options.config_dir`). |
