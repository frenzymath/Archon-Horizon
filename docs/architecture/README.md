# Architecture & Core Concepts

Archon Horizon is engineered specifically for orchestrating **Long Horizon Agents** in **Lean 4** autoformalization. Formalizing complex mathematics requires autonomous AI agents capable of sustained reasoning, proof repair loops, and compiler interaction over extended time horizons.

---

## Table of Contents

- [1. Two-Agent Collaboration Model](#1-two-agent-collaboration-model)
  - [Horizon Agent](#horizon-agent)
  - [Ground Agent](#ground-agent)
- [2. Multi-Project Workspace Foundation](#2-multi-project-workspace-foundation)
- [3. Harness Seam & Provider Routing](#3-harness-seam--provider-routing)
- [4. Native Subagents & Skills](#4-native-subagents--skills)

---

## 1. Two-Agent Collaboration Model

Archon Horizon divides cognitive responsibilities between two abstract, engine-agnostic agent roles operating across deterministic synchronization boundaries. Both roles are defined in [`agents/harness_agents.py`](../../src/archon_horizon/agents/harness_agents.py), driven by the prompts in [`agents/prompts.py`](../../src/archon_horizon/agents/prompts.py), and their reports are parsed by [`agents/parsing.py`](../../src/archon_horizon/agents/parsing.py).

```
+-----------------------------------------------------------------------------+
|                            Archon Horizon Workspace                         |
|                                                                             |
|   +-----------------------+   Collaboration   +-------------------------+   |
|   |     Ground Agent      | <---------------> |      Horizon Agent      |   |
|   |                       |       Rounds      |                         |   |
|   | • Blueprints & DAGs   |                   | • Autonomous Lean Proofs|   |
|   | • Roadmap & Memory    |                   | • Build & Tool Exec     |   |
|   | • Inbox Triage        |                   | • Proof Repair Loops    |   |
|   +-----------------------+                   +-------------------------+   |
+-----------------------------------------------------------------------------+
```

### Horizon Agent
The **Horizon Agent** acts as the dedicated autonomous formalization engine. Granted deep freedom by its prompts to tackle complex theorems over extended runs, its responsibilities include:
- Executing long-running proof searches and writing formal Lean 4 code directly within target projects.
- Running compiler toolchains (`lake build`), diagnosing error messages, and performing self-directed proof repair loops.
- Reporting formalization progress, partial proof milestones, and blockers back to the workspace via structured artifacts.
- Respecting standing protections (soft freezes) established by the Ground Agent or human supervisors.

### Ground Agent
The **Ground Agent** acts as the human-aligned supervisor, architect, and project manager. Constrained by targeted prompts to ensure human-grade clarity and organization, its responsibilities include:
- Maintaining and refining human-readable blueprints and LaTeX-subset dependency structures.
- Organizing roadmap milestones, breaking down high-level mathematical goals into concrete tasks.
- Triaging communication via local and GitHub inboxes.
- Summarizing progress, managing workspace memory, and preparing dashboard material.
- Invoking specialized native subagents (e.g., `blueprint-reviewer`, `diff-auditor`) for focused auditing.

---

## 2. Multi-Project Workspace Foundation

To support long-horizon agents working across interdependent mathematical libraries, Archon Horizon replaces single-repository tool chains with a collaborative workspace foundation (see [`core/workspace.py`](../../src/archon_horizon/core/workspace.py)):
- **Workspace**: The global collaboration root, managed via `config.yaml` and tracked inside an embedded `.archon-horizon/` directory. It maintains shared memory, overarching roadmaps, dependency graphs, and agent communication inboxes.
- **Projects**: Member Lean codebases within the workspace. Projects can be embedded subdirectories or managed as out-of-tree Git checkouts stored under `.archon-horizon/vcs/<project>.git` without relying on fragile Git submodules.

See the [Workspaces & Projects guide](../workspaces-and-projects/README.md) for the full layout.

---

## 3. Harness Seam & Provider Routing

Archon Horizon decouples high-level orchestration from the underlying LLM execution engine via a unified `Harness` interface, defined in [`harnesses/base.py`](../../src/archon_horizon/harnesses/base.py):

| Harness Type | Description |
| :--- | :--- |
| **CommandHarness** ([`command.py`](../../src/archon_horizon/harnesses/command.py)) | Launches external AI CLI tools as subprocesses. Configurable backends include **Claude Code** (`default`, `vscode`, `desktop`, or headless `claude-p`), **Codex** ([`codex.py`](../../src/archon_horizon/harnesses/codex.py)), or arbitrary custom shells. |
| **Provider Routing** | Built-in routing for **Kimi / Moonshot** and **DeepSeek** models, supporting direct API keys as well as automatic **OpenRouter** fallback. Configured in [`config/harnesses.py`](../../src/archon_horizon/config/harnesses.py). |
| **NullHarness** ([`null.py`](../../src/archon_horizon/harnesses/null.py)) | An in-process harness designed for rapid, deterministic integration testing and CI without making external network requests. |

---

## 4. Native Subagents & Skills

When a workspace is initialized or updated (`horizon init --reinit`, and again at the start of every run), Horizon compiles agent descriptors into native engine structures. The descriptors are provisioned by [`commands/subagent.py`](../../src/archon_horizon/commands/subagent.py) and [`commands/skills.py`](../../src/archon_horizon/commands/skills.py) during [`horizon init`](../../src/archon_horizon/commands/init.py), compiled per-engine by [`subagents/compile.py`](../../src/archon_horizon/subagents/compile.py), and MCP wiring lives in [`config/mcp.py`](../../src/archon_horizon/config/mcp.py):
- **Subagents** (both engines): Specialized roles such as `blueprint-reviewer` and `diff-auditor` are compiled to workspace-local `.claude/agents/<name>.md` (Claude) and `.codex/agents/<name>.toml` (Codex). Read-only is engine-enforced (Claude `disallowedTools`; Codex `sandbox_mode = "read-only"`).
- **Skills**: Modular capability guides are provisioned under `.claude/skills/<name>/SKILL.md`. **Claude Code** auto-discovers them. **Codex** has no such discovery, so Horizon inlines a skills index (names, descriptions, and the absolute `SKILL.md` paths to read on demand) into each compiled Codex agent, and the Ground/Horizon prompts point both engines at the same files.
