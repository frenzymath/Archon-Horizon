# Configuring `config.yaml`

Every workspace is driven by a single `config.yaml` at its root. It holds only **stable settings** — workspace defaults, harnesses (engines/models), member projects, external libraries, and freeze rules. Runtime state (tasks, runs, reports) lives under `.archon-horizon/` and is never edited by hand.

`config.yaml` is parsed and validated by [`config/loader.py`](../../src/archon_horizon/config/loader.py) against the schema in [`config/schema.py`](../../src/archon_horizon/config/schema.py). `horizon init` writes a starter file for you; this guide explains each section so you can extend it.

---

## Table of Contents

- [1. Minimal example](#1-minimal-example)
- [2. `workspace`](#2-workspace)
- [3. `harnesses`](#3-harnesses)
- [4. `projects`](#4-projects)
- [5. `external_libraries`](#5-external_libraries)
- [6. `github`](#6-github)
- [7. `freeze`](#7-freeze)
- [8. `references`](#8-references)

---

## 1. Minimal example

The file `horizon init` scaffolds looks roughly like this:

```yaml
workspace:
  name: my-workspace
  state_dir: .archon-horizon
  rounds: 1                       # max collaboration rounds per run
  ground_agent:
    harness: ground-default
  horizon_agent:
    harness: horizon-default
  scheduler:
    max_parallel_sessions: 1      # how many Horizon agents run at once

external_libraries:
  - name: mathlib
    rev: v4.x.0

harnesses:
  ground-default:
    kind: "claude-code"
    model: claude-opus-4-8
  horizon-default:
    kind: "claude-code"       # both agents default to Claude Code
    model: claude-opus-4-8
    # options are engine-specific — e.g. `effort` applies to Codex, not Claude Code

github:
  enabled: false
  repo:

projects: {}
```

---

## 2. `workspace`

Global defaults for the workspace and the collaboration loop (see [`WorkspaceConfig`](../../src/archon_horizon/config/schema.py)).

| Key | Default | Purpose |
| :--- | :--- | :--- |
| `name` | — (required) | Human-readable workspace name. |
| `state_dir` | `.archon-horizon` | Directory holding managed state (roadmap, inboxes, reports, search cache). |
| `rounds` | `1` | Maximum ground/horizon collaboration rounds per run. |
| `start_with` / `end_with` | `ground` | Which role opens/closes a run. Set to `horizon` to skip the opening or final reconcile Ground turn. |
| `ground_agent.harness` | — | Named harness (from `harnesses:`) that runs the Ground agent. |
| `horizon_agent.harness` | — | Named harness that runs the Horizon agent. |
| `ground_agent.subagents` | all | Restrict which native subagents Ground may dispatch. |
| `ground_agent.subagent_harness` | Ground's harness | Harness (and thus model) used to run Ground's subagents — point this at a cheaper or larger model. |
| `scheduler.max_parallel_sessions` | `1` | How many Horizon sessions run concurrently. |
| `scheduler.unknown_write_set_policy` | `lock-project` | What to do when a task's write set is unknown (see [`orchestration/scheduler.py`](../../src/archon_horizon/orchestration/scheduler.py)). |

---

## 3. `harnesses`

A harness is a named execution engine. Each `workspace` role and subagent points at one by name (see [`config/harnesses.py`](../../src/archon_horizon/config/harnesses.py) and the [Architecture guide](../architecture/README.md#3-harness-seam--provider-routing)).

```yaml
harnesses:
  ground-default:
    kind: "claude-code"         # claude-code | codex | command | null
    model: claude-opus-4-8
    models:                     # optional tier → concrete model map
      small: claude-haiku-4-5-20251001
      medium: claude-sonnet-5
    options:
      config_dir: ~/.claude-ground   # pin this harness to a specific account/home dir
  horizon-codex:
    kind: "codex"
    model: gpt-5.5
    options:
      effort: high              # backend-specific option
```

| Key | Purpose |
| :--- | :--- |
| `kind` | Engine type: `claude-code`, `codex`, a custom `command` (alias `external-agent`), or `null` (in-process, for tests). |
| `model` | Primary model — also the `big` tier default. |
| `models` | Optional map of symbolic tiers (`small`, `medium`, `big`) → concrete models. Must stay within the same provider. Native subagents resolve tiers through this. |
| `command` / `args` | For `kind: command`, the external CLI to launch and its arguments. |
| `options` | Backend-specific options — e.g. `effort` for Codex, or `config_dir` to pin auth/session home (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`). |
| `options.max_retries` | Transient API errors (rate limit, overload, 5xx, network) are retried with exponential backoff. Default `2` (i.e. 3 attempts); set `0` to disable. A usage/billing limit is never retried — it's labelled and surfaced. |
| `options.retry_base_seconds` | Base backoff delay in seconds for the retry above (default `8`; doubles each attempt). |

> [!TIP]
> Provider routing for Kimi / Moonshot, DeepSeek, and OpenRouter is configured through the harness `kind`/`model`/`options` here — see [`config/harnesses.py`](../../src/archon_horizon/config/harnesses.py). API keys go in the workspace-root `.env`, not `config.yaml`.

---

## 4. `projects`

Member Lean projects, keyed by name (see [`ProjectConfig`](../../src/archon_horizon/config/schema.py)). Usually managed with `horizon project add`, but editable by hand:

```yaml
projects:
  my-lib:
    path: ./my-lib             # embedded subdirectory, or a tracked checkout
    type: lean
    depends_on: [mathlib]
    blueprint:
      path: blueprint/src
    build:
      command: lake build
    vcs:
      enabled: true
      origin: https://github.com/me/my-lib.git
      branch: main
      git_dir: .archon-horizon/vcs/my-lib.git   # out-of-tree checkout
    freeze:
      files: ["MyLib/Basic.lean"]
      declarations: ["MyLib.foundational_def"]
    write_paths: ["MyLib/New/"]  # restrict where the agent may write
```

See the [Workspaces & Projects guide](../workspaces-and-projects/README.md) for embedded vs. out-of-tree tracking.

---

## 5. `external_libraries`

Lean dependencies the agents and the `horizon search` index should know about (see [`ExternalLibrary`](../../src/archon_horizon/config/schema.py)). `mathlib` is included by default.

```yaml
external_libraries:
  - name: mathlib               # known library — resolves automatically
    rev: v4.x.0
  - name: my-lib
    github: owner/my-lib        # clones https://github.com/owner/my-lib
    rev: main
  - name: vendored
    path: vendor/vendored       # already checked out; skip lake lookup
    git: https://example.com/vendored.git
```

A library resolves to a repo by precedence: explicit `git` url → `github` shorthand → `owner/repo` in the name → the built-in known-library table (so `name: mathlib` just works). The string shorthand `"owner/repo@rev"` is also accepted.

---

## 6. `github`

Enables shadow-syncing GitHub issues/PRs into your inbox via the `gh` CLI (see the [Inboxes guide](../inboxes-and-communication/README.md#3-github-inbox-integration)).

```yaml
github:
  enabled: true
  repo: owner/repo
  import_policy: labeled-only   # all | labeled-only | accepted-only
```

Imports are gated by the shared triage labels — `agent-ready`, `not-ready`, `rejected` (defined in [`core/labels.py`](../../src/archon_horizon/core/labels.py)):

| `import_policy` | Imports |
| :--- | :--- |
| `all` | Every issue/PR. |
| `labeled-only` (default) | Items carrying any triage label. |
| `accepted-only` | Only items labeled `agent-ready`. |

---

## 7. `freeze`

Workspace-wide standing protections applied before every dispatch (see [`core/freeze.py`](../../src/archon_horizon/core/freeze.py)). Per-project freezes live under each project's `freeze:` block instead.

```yaml
freeze:
  agents: []                    # agents that may never write
  projects: [foundations]       # whole projects held read-only
  files: ["Core/Api.lean"]
  declarations: ["Core.Api.signature"]
  blueprint_nodes: ["thm:main_result"]
```

Ad-hoc, per-run protections can also be added with `horizon inbox protect` — see the [Inboxes guide](../inboxes-and-communication/README.md#2-standing-protections-soft-freeze).

---

## 8. `references`

Optional override for page-level PDF reference transcription. Defaults to the Ground subagent harness; set both fields to pin a cheap vision-capable model.

```yaml
references:
  transcription:
    harness: ground-default
    model: <vision-capable-model>
```

---

Secrets (API keys, tokens) belong in the workspace-root `.env` file, not `config.yaml` — see [`config/env.py`](../../src/archon_horizon/config/env.py). `horizon init` scaffolds a template `.env` for you.
