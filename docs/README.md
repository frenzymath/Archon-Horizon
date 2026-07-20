# Archon Horizon Documentation

In-depth guides for [**Archon Horizon**](../README.md) — workspace-first orchestration for long-horizon Lean 4 formalization agents. Each topic lives in its own subfolder, and each guide links inline to the relevant source files under [`src/archon_horizon/`](../src/archon_horizon) so you (or an agent) can jump straight to the implementation.

> [!TIP]
> New here? Start with the [main README](../README.md) for the elevator pitch and Quick Start, then dive into the guides below.

---

## Table of Contents

- [Guides](#guides)
- [Command map](#command-map)

---

## Guides

| Guide | Covers | Key commands |
| :--- | :--- | :--- |
| 🏗️ [Architecture & Core Concepts](./architecture/README.md) | The Horizon loop, fresh-context Ground helper, multi-project workspaces, harness execution seams, and native subagents. | — |
| 📁 [Workspaces & Projects](./workspaces-and-projects/README.md) | Scaffolding a workspace, embedded vs. out-of-tree Git projects, and pre-dispatch freeze protections. | `horizon init`, `horizon project` |
| 🔧 [Configuration](./configuration/README.md) | Every `config.yaml` section — workspace defaults, harnesses/models, projects, external libraries, GitHub, and freeze rules. | — |
| ⚙️ [Orchestration & Roadmap](./orchestration-and-roadmap/README.md) | The multi-round collaboration loop, running tasks, milestones, and structured execution reports. | `horizon run`, `horizon roadmap`, `horizon task` |
| 💬 [Inboxes & Communication](./inboxes-and-communication/README.md) | Async local inboxes, standing protections (soft freezes), and GitHub CLI shadow sync. | `horizon inbox`, `horizon sync` |
| 📐 [Blueprints & semantic graphs](./blueprints-and-graph/README.md) | Synchronizing blueprints and Lean into the vendored graph and using the chapter Graphviz view. | `horizon blueprint`, `horizon graph` |
| 🖥️ [Dashboard & Search](./dashboard-and-search/README.md) | The live web dashboard, static export for GitHub Pages, and offline declaration search. | `horizon dashboard`, `horizon search` |
| 💻 [CLI Reference](./cli-reference/README.md) | Complete command and flag reference, plus `--json` machine-readable output. | all |
| 📝 [Changelog](./CHANGELOG.md) | Version release notes and migration instructions. | — |

---

## Command map

Every command supports `--json` for machine-readable output on `stdout`.

| Command | Purpose |
| :--- | :--- |
| `horizon init` | Scaffold a workspace, or refresh managed subagents/skills (`--update`). |
| `horizon setup` | Check and configure external tools (Claude Code, Lean 4 toolchain, …). |
| `horizon update` | Upgrade the Archon Horizon package. |
| `horizon run <target>` | Run autoformalization on a task/project, everything (`*`), or an interactive Horizon session. |
| `horizon discuss` | Interactive session to talk with the workspace (status, recent runs, guided edits). |
| `horizon roadmap` | Manage milestones and the overarching roadmap. |
| `horizon task` | Create and manage formalization tasks. |
| `horizon project` | Add and manage member Lean projects. |
| `horizon inbox` | Manage local hints, issues, and standing protections. |
| `horizon sync` | Shadow-sync GitHub issues and pull requests via the `gh` CLI. |
| `horizon blueprint` | Extract and inspect LaTeX blueprints. |
| `horizon graph` | Synchronize, query, and annotate a project's semantic graph. |
| `horizon search <query>` | Offline search for lemmas/definitions in Mathlib and local projects. |
| `horizon dashboard` | Run the live web server or export static HTML (`--static`). |
| `horizon skills` | Manage workspace-local skill files. |

See the [CLI Reference](./cli-reference/README.md) for flags and advanced options.
