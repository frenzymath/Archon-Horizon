# Configuring `config.yaml`

Every workspace is driven by a single `config.yaml` at its root. It holds only **stable settings** — workspace defaults, harnesses (engines/models), member projects, external libraries, and freeze rules. Runtime state (tasks, runs, reports) lives under `.archon-horizon/` and is never edited by hand.

`config.yaml` is parsed and validated by [`config/loader.py`](../../src/archon_horizon/config/loader.py) against the schema in [`config/schema.py`](../../src/archon_horizon/config/schema.py). `horizon init` writes a starter file for you; this guide explains each section so you can extend it.

---

## Table of Contents

- [1. Minimal example](#1-minimal-example)
- [2. `workspace`](#2-workspace)
  - [`workspace.delegation`](#workspacedelegation)
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
  rounds: 1                       # max Horizon sessions per run
  horizon_agent:
    harness: horizon-default
  scheduler:
    max_parallel_sessions: 1      # how many Horizon agents run at once

external_libraries:
  - name: mathlib
    rev: v4.x.0

harnesses:
  horizon-default:
    kind: "claude-code"       # primary Horizon session engine
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
| `rounds` | `1` | Maximum Horizon sessions per run. Long runs schedule the read-only `ground` helper at convergence checkpoints. |
| `horizon_agent.harness` | — | Named harness (from `harnesses:`) that runs the Horizon agent. |
| `scheduler.max_parallel_sessions` | `1` | How many Horizon sessions run concurrently. |
| `scheduler.unknown_write_set_policy` | `lock-project` | What to do when a task's write set is unknown (see [`orchestration/scheduler.py`](../../src/archon_horizon/orchestration/scheduler.py)). |

### `workspace.delegation`

The standing **delegation consent** a running agent reads before it delegates
work it cannot do inline — creating new tasks, or launching a whole new
`horizon run` session (see [`DelegationConfig`](../../src/archon_horizon/config/schema.py)).
**The default is deny**: an agent may not create tasks or launch runs. This
block is the user's consent *record*; reading it authorizes nothing on its own —
any actuator that acts on it (spawning a run, selecting an account) is a
separate, deliberately-gated capability.

```yaml
workspace:
  delegation:
    allow_launch_tasks: true         # agent may create new tasks in the store
    allow_launch_runs: false         # agent may spawn a new `horizon run`
    max_parallel_sessions: 2         # cap on agent-launched concurrent runs (0 = none)
    accounts:                        # free-form descriptors to choose among
      - name: primary
        config_dir: ~/.claude-primary # per-harness account/home (see below)
        notes: "default; resets 00:00 UTC"
      - name: overflow
        config_dir: ~/.claude-overflow
    # any other keys you write are preserved verbatim for the agent to read
    api_notes: "use overflow only after primary hits its limit"
```

| Key | Default | Purpose |
| :--- | :--- | :--- |
| `allow_launch_tasks` | `false` | Whether the agent may create new tasks in the store. |
| `allow_launch_runs` | `false` | Whether the agent may spawn a new `horizon run`. |
| `max_parallel_sessions` | `0` | Cap on agent-launched concurrent runs (`0` = none). |
| `accounts` | `[]` | Free-form list of account/api descriptors — e.g. per-harness `config_dir` account homes plus notes — for the agent to choose among. |
| *(other keys)* | — | Any additional keys are preserved verbatim under `raw`, so the agent can read and reason about account notes, limit-reset times, or api-key hints. |

Agents read this block via **`horizon permissions`** (add `--json` for the
machine-readable form; see [`commands/permissions.py`](../../src/archon_horizon/commands/permissions.py)),
which prints the typed fields, any free-form notes, and — when delegation is off
— an explicit reminder not to create tasks or launch runs.

The `accounts` descriptors compose with the per-harness `config_dir` option
([section 3](#3-harnesses)): `config_dir` pins one harness to a single auth/session
home (`CLAUDE_CONFIG_DIR` / `CODEX_HOME`), while `accounts` records the set of
account homes an agent may pick among when it launches further work.

---

## 3. `harnesses`

A harness is a named execution engine. The workspace selects one for its Horizon sessions; native helpers inherit that session by default, and Horizon may choose a per-dispatch override when the engine supports one (see [`config/harnesses.py`](../../src/archon_horizon/config/harnesses.py) and the [Architecture guide](../architecture/README.md#4-harness-seam--provider-routing)).

```yaml
harnesses:
  horizon-default:
    kind: "claude-code"         # claude-code | codex | command | null
    model: claude-opus-4-8
  horizon-codex:
    kind: "codex"
    model: gpt-5.5
    options:
      effort: high              # backend-specific option
```

| Key | Purpose |
| :--- | :--- |
| `kind` | Engine type: `claude-code`, `codex`, a custom `command` (alias `external-agent`), or `null` (in-process, for tests). |
| `model` | Primary model for the Horizon session. Helpers inherit by default; the Horizon agent chooses any per-dispatch override through the native engine. |
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

Use `horizon freeze add <kind> <pattern>`, `horizon freeze list`, and
`horizon freeze remove <kind> <pattern>` to maintain these rules without editing
YAML. Supported kinds are `agent`, `project`, `file`, `declaration`, and
`blueprint-node`.

Semantic protections that cannot be expressed as an exact target/glob can be
added with `horizon inbox protect`; those guide the agent but are not the
pre-dispatch enforcement mechanism. See the [Inboxes guide](../inboxes-and-communication/README.md#2-standing-protections-soft-freeze).

---

## 8. `references`

Page transcription is dispatched by the Horizon agent with a suitable
vision-capable model and effort. Model selection is deliberately not encoded in
`config.yaml` or helper descriptors.

---

Secrets (API keys, tokens) belong in the workspace-root `.env` file, not `config.yaml` — see [`config/env.py`](../../src/archon_horizon/config/env.py). `horizon init` scaffolds a template `.env` for you.
