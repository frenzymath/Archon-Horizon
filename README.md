<div align="center">

# Archon Horizon

**Frenzymath · PKU@AI4Math**

*Workspace-first orchestration for long-horizon Lean 4 formalization agents*

![Version](https://img.shields.io/badge/version-0.1.0-blue)
[![License](https://img.shields.io/badge/Apache-2.0-green)](./LICENSE)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Lean](https://img.shields.io/badge/domain-Lean%204-1f6feb)

</div>

---

Archon Horizon orchestrates autonomous AI agents that formalize mathematics in **Lean 4** across **multiple interdependent projects**. A workspace is the unit of work: agents plan over shared blueprints and dependency graphs, then run long, self-directed proving sessions, building with `lake`, diagnosing compiler errors, and repairing proofs without constant human supervision. While autonomous by design, Archon Horizon also integrates better human-agent collaboration locally, and external collaboration with GitHub.

Two roles divide the work:

- **Ground Agent** — the constrained strategist. Maintains LaTeX blueprints, dependency DAGs, roadmaps, inboxes, human-readable artifacts, and prevents the **Horizon Agent** from diverging. 
- **Horizon Agent** — the autonomous prover. Writes Lean, writes blueprints, runs toolchains, repairs failures, organizes the workspace, creates subprojects, etc.

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
- [License](#license)

---

## Features

- **Two-agent orchestration.** A Ground Agent plans over blueprints, roadmaps, and inboxes and keeps the run on track, while an autonomous Horizon Agent does the actual Lean proving over long, self-directed sessions. The two alternate across configurable collaboration rounds, keeping strategy and execution cleanly separated. → [Architecture](./docs/architecture/README.md)
- **Multi-engine harnesses.** Orchestration is decoupled from the execution engine behind a single `Harness` seam, so agents can run on Claude Code, OpenAI Codex, or any custom command. Kimi/Moonshot, DeepSeek, and OpenRouter routing are built in, and a `null` harness keeps tests offline. → [Architecture](./docs/architecture/README.md#3-harness-seam--provider-routing)
- **Multi-project workspaces.** A workspace holds many interdependent Lean projects under one root — embedded as subdirectories or tracked as out-of-tree Git checkouts (no fragile submodules). Everything (config, models, freezes) is declared in a single [`config.yaml`](./docs/configuration/README.md). → [Workspaces & Projects](./docs/workspaces-and-projects/README.md)
- **Dual inboxes & standing protections.** Humans and agents collaborate asynchronously through a local filesystem inbox and an optional GitHub shadow-sync, observed only at round boundaries so proof searches are never interrupted mid-flight. Standing protections soft-freeze foundational signatures and files so autonomous runs can't quietly break your API. → [Inboxes](./docs/inboxes-and-communication/README.md)
- **Blueprints & dependency graphs.** LaTeX-subset blueprints are parsed into structured dependency DAGs linking informal statements to their Lean declarations. The embedded `leandag` engine queries dependency cones, reverse dependencies, and unions/intersections across projects — fully offline. → [Blueprints & leandag](./docs/blueprints-and-leandag/README.md)
- **Dashboard & offline search.** A live web dashboard renders the DAG (KaTeX), run logs, and inbox, and can export a self-contained static snapshot for GitHub Pages. `horizon search` runs BM25, Loogle-style name, and signature-pattern search over `.lean` sources with no GPU, API key, or network. → [Dashboard & Search](./docs/dashboard-and-search/README.md)

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
