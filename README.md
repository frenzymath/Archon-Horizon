<div align="center">

# Archon Horizon

**Frenzymath · PKU@AI4Math**

*Workspace-first orchestration for long-horizon Lean 4 formalization agents*

![Version](https://img.shields.io/badge/version-0.1.2-blue)
[![License](https://img.shields.io/badge/Apache-2.0-green)](./LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Lean](https://img.shields.io/badge/domain-Lean%204-1f6feb)

</div>

---

Archon Horizon orchestrates autonomous AI agents that formalize mathematics in **Lean 4** across **multiple interdependent projects**. A workspace is the unit of work: the Horizon agent plans over shared blueprints and dependency graphs, runs long proving sessions, builds with `lake`, and repairs failures without constant human supervision. Fresh-context helpers provide independent review and workspace hygiene without creating a second orchestration loop.

The **Horizon Agent** owns the proof loop and decides when to dispatch helpers. The read-only **Ground** helper is a scheduled workspace-wide checkpoint for strategy, graph/task consistency, ledger hygiene, and convergence; `work-reviewer`, `blueprint`, `janitor`, and the other helpers remain available for narrower slices.

> [!NOTE]
> Archon Horizon is the successor to [**Archon**](https://github.com/frenzymath/Archon), which formalizes research-level mathematics within a *single* project. Horizon generalizes that model to **workspaces of many projects**. The main argument is that LLMs will able to maintain larger and larger formalization projects, **Archon Horizon** will be able to orchestrate them in a scalable workspace.

> 📚 Full guides live in [`docs/`](./docs/README.md). 📝 Release notes in [`docs/CHANGELOG.md`](./docs/CHANGELOG.md).

---

## Table of Contents

- [Features](#features)
- [Quick Start](#quick-start)
  - [1. Install](#1-install)
  - [2. Scaffold a workspace](#2-scaffold-a-workspace)
  - [3. Run](#3-run)
- [CLI Overview](#cli-overview)
- [Live Demo](#live-demo)
- [License](#license)

---

## Features

- **Fresh-context convergence checks.** The Horizon agent schedules the read-only Ground helper before terminal task completion and during long runs, with narrower `work-reviewer` and `janitor` helpers available as needed. → [Architecture](./docs/architecture/README.md)
- **Multi-engine harnesses.** Orchestration is decoupled from the execution engine behind a single `Harness` seam, so agents can run on Claude Code, OpenAI Codex, or any custom command. Kimi/Moonshot, DeepSeek, and OpenRouter routing are built in, and a `null` harness keeps tests offline. → [Architecture](./docs/architecture/README.md#4-harness-seam--provider-routing)
- **Multi-project workspaces.** A workspace holds many interdependent Lean projects under one root — embedded as subdirectories or tracked as out-of-tree Git checkouts (no fragile submodules). Everything (config, models, freezes) is declared in a single [`config.yaml`](./docs/configuration/README.md). → [Workspaces & Projects](./docs/workspaces-and-projects/README.md)
- **Async inboxes, per-team ownership & standing protections.** Humans and agents collaborate through a local filesystem inbox and an optional GitHub shadow-sync, observed only at round boundaries so proof searches are never interrupted mid-flight. Items can be shared or **owned by one task** (a private per-team inbox), carry **per-team read-state** (`inbox read`/`unread`, `list --mine/--unread`), and be **direct-messaged** to another running team. Standing protections soft-freeze foundational signatures and files so autonomous runs can't quietly break your API. → [Inboxes](./docs/inboxes-and-communication/README.md)
- **Parallel teams on a shared board.** Each `horizon run` is a team (a lead agent plus its subagent workers); parallel teams coordinate through shared state — a roadmap that doubles as a **project board** (owner, milestone labels, pinned commits, a live `/board` view), the inbox, and the commit ledger — rather than synchronous meetings. A stderr **synchronizer** keeps each agent aware of unread messages and other live runs, and launching work for *other* teams is gated by an opt-in `workspace.delegation` policy (default deny). → [Architecture](./docs/architecture/README.md)
- **Blueprints & dependency graphs.** LaTeX-subset blueprints and Lean sources synchronize into a vendored, plain-files semantic graph. `horizon graph` exposes frontier, dependency, review, and comment operations; the dashboard renders a deterministic chapter-collapsed Graphviz view. → [Blueprints & semantic graphs](./docs/blueprints-and-graph/README.md)
- **Dashboard & offline search.** A live web dashboard renders the DAG (KaTeX), run logs, and inbox, and can export a self-contained static snapshot for GitHub Pages. `horizon search` runs BM25, Loogle-style name, and signature-pattern search over `.lean` sources with no GPU, API key, or network. → [Dashboard & Search](./docs/dashboard-and-search/README.md)
- **Public demo workspace.** A tiny two-chapter Lean/blueprint fixture ships outside the Python package with synthetic Claude Code and Codex runs, so the dashboard can be explored without credentials. → [Open the live demo](https://axeldlv00.github.io/Archon-Horizon/)

*More depth on every topic — including the full [`config.yaml`](./docs/configuration/README.md) reference — lives in [`docs/`](./docs/README.md).*

---

## Quick Start

### 1. Install

The recommended way is running the one-liner below, inside a Python 3.11+ virtual environment:

```bash
# One-liner: fetch latest main, install, and run tool checks
curl -sSL https://raw.githubusercontent.com/frenzymath/Archon-Horizon/refs/heads/main/install.sh | bash
```

If you prefer to install from source:

```bash
git clone https://github.com/frenzymath/Archon-Horizon.git
cd Archon-Horizon
python -m pip install .
horizon setup        # Check external tools (Claude Code, Lean 4 toolchain, …)
```

> [!WARNING]
> Horizon lets AI engines run terminal commands. Prefer a dedicated non-root user, a Docker container, or a VM. When running Claude Code as root, you may need `IS_SANDBOX=1`.

### 2. Scaffold a workspace

```bash
mkdir my-workspace && cd my-workspace
horizon init
```

`init` walks you through `config.yaml`, adding member Lean projects, and structuring your first tasks. 

👉 See the [Workspaces guide](./docs/workspaces-and-projects/README.md) and the [Configuration guide](./docs/configuration/README.md) for `config.yaml`.

### Live Demo

The [public demo](https://axeldlv00.github.io/Archon-Horizon/) is rebuilt by
GitHub Actions from [`demo/`](./demo/). It is intentionally small: two blueprint
chapters, partial Lean coverage, one open issue, and two historical runs showing
different engines and a Ground review checkpoint.

### 3. Run

```bash
horizon run my_task           # a single task or project
horizon run '*'               # every member project
horizon dashboard             # live progress at http://127.0.0.1:8765
```

> [!TIP]
> Keep Horizon current with `horizon update` (upgrade the package) followed by `horizon init --update` (refresh managed subagents and skills inside a workspace).

---

## CLI Overview

Every command supports `--json` for machine-readable output on `stdout`. Run `horizon -h` for all commands, and `horizon <command> -h` for command-specific flags.

- `horizon init` — scaffold a workspace or refresh managed files (`--update`).
- `horizon run <target>` — run autoformalization on a task or project, everything (`*`), or a single role (`ground` / `horizon`, optionally `--backend interactive`).
- `horizon discuss` — open an interactive session to talk with the workspace: status, recent runs, and edits to projects/tasks/inbox/roadmap on request.
- `horizon dashboard` — live server, or static HTML export (`--static`).

👉 See the [full CLI reference](./docs/cli-reference/README.md) for all commands and flags.

---

## License

Licensed under the [Apache License 2.0](./LICENSE). Third-party attributions are in [THIRD_PARTY_NOTICES.md](./THIRD_PARTY_NOTICES.md).
