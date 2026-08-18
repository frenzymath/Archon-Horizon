# Architecture & Core Concepts

Archon Horizon is engineered specifically for orchestrating **Long Horizon Agents** in **Lean 4** autoformalization. Formalizing complex mathematics requires autonomous AI agents capable of sustained reasoning, proof repair loops, and compiler interaction over extended time horizons.

---

## Table of Contents

- [1. Horizon Loop & Fresh-Context Review](#1-horizon-loop--fresh-context-review)
  - [Horizon Agent](#horizon-agent)
  - [Ground Helper](#ground-helper)
- [2. Multi-Project Workspace Foundation](#2-multi-project-workspace-foundation)
- [3. Teams & the Shared Workspace](#3-teams--the-shared-workspace)
  - [Coordinating Through Shared State](#coordinating-through-shared-state)
  - [The Pre-Command Synchronizer](#the-pre-command-synchronizer)
  - [Delegation Is Off by Default](#delegation-is-off-by-default)
- [4. Harness Seam & Provider Routing](#4-harness-seam--provider-routing)
- [5. Native Subagents & Skills](#5-native-subagents--skills)

---

## 1. Horizon Loop & Fresh-Context Review

Archon Horizon has one orchestrated driver: the Horizon agent. It owns the
proof loop, workspace strategy, and dispatch decisions. Independent review is a
native subagent checkpoint, not a second alternating orchestrator role.

```
+-----------------------------------------------------------------------------+
|                            Archon Horizon Workspace                         |
|                                                                             |
|   +-----------------------+   Collaboration   +-------------------------+   |
|   |   Ground helper (review) | <--- native dispatch --- |   Horizon Agent   |   |
|   |                       |       Rounds      |                         |   |
|   | • Fresh strategy view |                   | • Autonomous Lean Proofs|   |
|   | • Graph/task hygiene  |                   | • Build & Tool Exec     |   |
|   | • Issues & memory     |                   | • Proof Repair Loops    |   |
|   +-----------------------+                   +-------------------------+   |
+-----------------------------------------------------------------------------+
```

### Horizon Agent
The **Horizon Agent** acts as the dedicated autonomous formalization engine. Granted deep freedom by its prompts to tackle complex theorems over extended runs, its responsibilities include:
- Executing long-running proof searches and writing formal Lean 4 code directly within target projects.
- Running compiler toolchains (`lake build`), diagnosing error messages, and performing self-directed proof repair loops.
- Reporting formalization progress, partial proof milestones, and blockers back to the workspace via structured artifacts.
- Respecting standing protections (soft freezes) established by helpers or human supervisors.

### Ground Helper
The read-only **Ground** helper is dispatched by Horizon before terminal task
completion, after long stretches of work, and after strategy pivots. It rebuilds
context from the ledger diff, reports, roadmap, inbox, graph, and Lean state and
answers whether the workspace is converging. It files concise issues or memory
items but does not edit source or mark tasks done. `work-reviewer` handles a
narrow proof/diff audit; `janitor` handles documentation and inbox hygiene.

---

## 2. Multi-Project Workspace Foundation

To support long-horizon agents working across interdependent mathematical libraries, Archon Horizon replaces single-repository tool chains with a collaborative workspace foundation (see [`core/workspace.py`](../../src/archon_horizon/core/workspace.py)):
- **Workspace**: The global collaboration root, managed via `config.yaml` and tracked inside an embedded `.archon-horizon/` directory. It maintains shared memory, overarching roadmaps, dependency graphs, and agent communication inboxes.
- **Projects**: Member Lean codebases within the workspace. Projects can be embedded subdirectories or managed as out-of-tree Git checkouts stored under `.archon-horizon/vcs/<project>.git` without relying on fragile Git submodules.

See the [Workspaces & Projects guide](../workspaces-and-projects/README.md) for the full layout.

---

## 3. Teams & the Shared Workspace

Each `horizon run` is one **team**: a lead **Horizon agent** that dispatches native **subagent workers** (`ground`, `work-reviewer`, `janitor`, ...) to do bounded work on its behalf. Several teams can be live on the same workspace at once — one per run — and they do **not** coordinate synchronously. Instead they coordinate through **shared state** on disk, reading and writing the same durable surfaces.

### Coordinating Through Shared State

- **The roadmap / project board** — the workspace's shared map of mathematical status and who owns what, updated as work lands. Who is *currently* running an item is derived live from running tasks, not stored. See the [Orchestration & Roadmap guide](../orchestration-and-roadmap/README.md#3-roadmaps-vs-tasks).
- **The inbox** — asynchronous messages, long-lived **per-team memory**, and **direct messages** addressed to a single task or run, so one team can hand off, warn, or leave a note for another without either side being online. See the [Inboxes & Communication guide](../inboxes-and-communication/README.md).
- **The commit ledger** — the out-of-tree git history every team commits into, carrying rich provenance trailers, which is the authoritative record of what changed. See the [Workspaces & Projects guide](../workspaces-and-projects/README.md).

Other live runs are never invisible: `horizon ps` lists them from the per-run process markers ([`commands/ps.py`](../../src/archon_horizon/commands/ps.py)), and the synchronizer surfaces the same signal at command start.

### The Pre-Command Synchronizer

So a team stays aware of shared state without polling, every `horizon` CLI invocation *inside an agent session* first prints a short, cached, **stderr-only** digest — the **synchronizer** ([`core/synchronizer.py`](../../src/archon_horizon/core/synchronizer.py), wired into the app callback in [`cli.py`](../../src/archon_horizon/cli.py)). It surfaces:

- unread inbox items for the current task (owned and shared),
- the session's runtime and cumulative tokens — a soft nudge to compact or wrap up a large session,
- any other runs live on the same workspace.

It writes only to stderr, so `--json` stdout stays clean, and it is fully best-effort — any failure is swallowed and can never break a command. A short cache TTL keeps rapid successive commands from rescanning, and it is skipped outside an agent session or with `--no-sync` / `ARCHON_HORIZON_NO_SYNC=1`.

### Delegation Is Off by Default

A team may spawn subagent workers **within itself** freely — that is ordinary dispatch. Launching work for **other** teams is different: creating new tasks, or spawning a whole new `horizon run`, requires the human's standing consent recorded under `workspace.delegation` in `config.yaml`, and the **default is deny**. The agent reads this consent record with [`horizon permissions`](../../src/archon_horizon/commands/permissions.py) (typed by `DelegationConfig` in [`config/schema.py`](../../src/archon_horizon/config/schema.py)) before delegating; reading it authorizes nothing on its own — any actuator that acts on it is a separate, deliberately gated capability. See the [Configuration guide](../configuration/README.md#workspacedelegation).

---

## 4. Harness Seam & Provider Routing

Archon Horizon decouples high-level orchestration from the underlying LLM execution engine via a unified `Harness` interface, defined in [`harnesses/base.py`](../../src/archon_horizon/harnesses/base.py):

| Harness Type | Description |
| :--- | :--- |
| **CommandHarness** ([`command.py`](../../src/archon_horizon/harnesses/command.py)) | Launches external AI CLI tools as subprocesses. Configurable backends include **Claude Code** (`default`, `vscode`, `desktop`, or headless `claude-p`), **Codex** ([`codex.py`](../../src/archon_horizon/harnesses/codex.py)), or arbitrary custom shells. |
| **Provider Routing** | Built-in routing for **Kimi / Moonshot** and **DeepSeek** models, supporting direct API keys as well as automatic **OpenRouter** fallback. Configured in [`config/harnesses.py`](../../src/archon_horizon/config/harnesses.py). |
| **NullHarness** ([`null.py`](../../src/archon_horizon/harnesses/null.py)) | An in-process harness designed for rapid, deterministic integration testing and CI without making external network requests. |

---

## 5. Native Subagents & Skills

When a workspace is initialized or updated (`horizon init --update`, and again at the start of every run), Horizon compiles agent descriptors into native engine structures. The descriptors are provisioned by [`commands/subagent.py`](../../src/archon_horizon/commands/subagent.py) and [`commands/skills.py`](../../src/archon_horizon/commands/skills.py) during [`horizon init`](../../src/archon_horizon/commands/init.py), compiled per-engine by [`subagents/compile.py`](../../src/archon_horizon/subagents/compile.py), and MCP wiring lives in [`config/mcp.py`](../../src/archon_horizon/config/mcp.py):
- **Subagents** (both engines): Specialized roles such as `ground`, `work-reviewer`, and `blueprint` are compiled to workspace-local `.claude/agents/<name>.md` (Claude) and `.codex/agents/<name>.toml` (Codex). Read-only is engine-enforced (Claude `disallowedTools`; Codex `sandbox_mode = "read-only"`).
- **Model ownership:** descriptors do not pin a model, tier, or effort. Helpers inherit the parent by default; Horizon chooses a lighter capable model for mechanical work or the same/high-effort model for mathematical review through the engine's native dispatch mechanism.
- **Skills**: Modular capability guides are provisioned under `.claude/skills/<name>/SKILL.md`. **Claude Code** auto-discovers them. **Codex** has no such discovery, so Horizon inlines a skills index (names, descriptions, and the absolute `SKILL.md` paths to read on demand) into each compiled Codex agent.

Claude reports child events inline. Headless Codex instead writes every native
child to its own rollout file, so [`CodexHarness`](../../src/archon_horizon/harnesses/codex.py)
tails the rollout store, discovers direct and nested children from
`thread_spawn` provenance, assigns source-line event ids for idempotent
reconciliation, and normalizes child lifecycle, token, and compaction events
into the backend-neutral transcript model.
