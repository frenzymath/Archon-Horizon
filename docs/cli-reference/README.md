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
| `--reinit` | Refresh managed artifacts (subagents, skills, MCP, git excludes) on an existing workspace; preserves your `config.yaml` and content. Non-prompting. |

### `horizon setup`
Check (and offer to install) external tools: the configured engines, the Lean 4 toolchain (`elan`/`lake`), and `gh`. ([`commands/setup.py`](../../src/archon_horizon/commands/setup.py))

### `horizon update`
Upgrade the installed `archon-horizon` package. Follow with `horizon init --reinit` in each workspace. ([`commands/update.py`](../../src/archon_horizon/commands/update.py))

---

## 3. Execution & orchestration

### `horizon run <target>`
Drive a Ground/Horizon collaboration run. ([`commands/run.py`](../../src/archon_horizon/commands/run.py))

**Targets** (positional):

| Target | Effect |
| :--- | :--- |
| `.` | Ad-hoc task over **all** configured projects. |
| `'*'` | Every member project (must be the only target). |
| `<task-id>` | Pin a specific task. |
| `<project>` / `<file>` | Synthesize an ad-hoc task scoped to that project/file. |
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

### `horizon discuss`
Open an **interactive** session with the workspace (using the Ground harness). The agent reads the docs and on-disk state, summarizes status and recent runs, and can manage projects/tasks/inbox/roadmap — but only modifies things when you explicitly ask. ([`commands/discuss.py`](../../src/archon_horizon/commands/discuss.py))

### `horizon sync`
Sync inbox providers — e.g. import GitHub issue/PR shadows into the local inbox via the `gh` CLI (see the `github:` config). ([`commands/sync.py`](../../src/archon_horizon/commands/sync.py))

---

## 4. Inbox & communication

Implemented in [`commands/inbox.py`](../../src/archon_horizon/commands/inbox.py).

- `horizon inbox list` — show active inbox items.
- `horizon inbox add --kind <k> --body <text> [--project P] [--to ROLE] [--author A] [--persistent|--temporary] [--pending]` — add a hint/issue.
- `horizon inbox comment <id> --body <text> [--author A]` — post a comment.
- `horizon inbox protect [--file F] [--declaration D] [--project P] --body <why>` — add a standing soft-freeze protection.
- `horizon inbox edit <id> [--body B] [--kind K]` · `edit-comment <id> --index N --body B`.
- `horizon inbox label <id> <labels>` · `complete <id>` · `reject <id>` · `delete <id>`.

---

## 5. Roadmap, tasks & projects

**Roadmap** — the agent-managed work plan ([`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py)):
`list` · `add --id --title --project [--summary|--summary-file --status --kind --priority]` · `set <id> [--status …]` · `comment <id> --body` · `remove <id>`.

**Tasks** — human-authored (agents may read/comment, not author) ([`commands/task.py`](../../src/archon_horizon/commands/task.py)):
`list` · `show <id>` · `add --id --project --objective [--title --projects --file --priority --status]` · `set <id> [--status --priority --objective --title]` · `comment <id> --body` · `remove <id>`.

**Projects** ([`commands/project.py`](../../src/archon_horizon/commands/project.py)):
`add <name> <path> [--type --build]` · `archive <name>` · `remove <name>` · `merge <dest> <source>`.

**Skills** ([`commands/skills.py`](../../src/archon_horizon/commands/skills.py)): `list` · `install`.

---

## 6. Blueprints & search

- `horizon blueprint [--json]` — parse LaTeX blueprints and write JSON DAGs to `.archon-horizon/blueprints/`. ([`commands/blueprint.py`](../../src/archon_horizon/commands/blueprint.py))
- `horizon leandag [-p PROJECT] [--node <id>] [--cone] [--union] [--intersect]` — inspect dependency cones, dependents, and multi-project unions/intersections. ([`commands/leandag.py`](../../src/archon_horizon/commands/leandag.py))
- `horizon search <query> [--name P] [--type SIG] [--lib L] [--limit N] [--reindex]` — offline BM25 / name / signature search over Mathlib and local `.lean` sources. ([`commands/search.py`](../../src/archon_horizon/commands/search.py))

---

## 7. Dashboard & export

[`commands/dashboard.py`](../../src/archon_horizon/commands/dashboard.py)

- `horizon dashboard [--host --port --dist]` — live web server (default `127.0.0.1:8765`).
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
