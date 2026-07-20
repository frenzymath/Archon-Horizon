# Orchestration & Roadmap Guide

Archon Horizon manages complex mathematical formalization through structured orchestration, coordinating autonomous proof work via explicit tasks, multi-round collaboration loops, and persistent roadmaps. The loop itself is driven by [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py) and [`orchestration/scheduler.py`](../../src/archon_horizon/orchestration/scheduler.py).

---

## Table of Contents

- [1. Running Autoformalization Tasks](#1-running-autoformalization-tasks)
  - [Dry Runs and Resumption](#dry-runs-and-resumption)
- [2. Collaboration Rounds](#2-collaboration-rounds)
- [3. Roadmaps vs. Tasks](#3-roadmaps-vs-tasks)
  - [Common Roadmap Commands](#common-roadmap-commands)
- [4. Reports, Events & Memory](#4-reports-events--memory)

---

## 1. Running Autoformalization Tasks

The core execution command in Archon Horizon is `horizon run` (see [`commands/run.py`](../../src/archon_horizon/commands/run.py)). It dispatches the underlying agent engine against specified targets within the workspace:

# Each target resolves in order **task id → roadmap item id → project/file**:

```bash
# Run a specific human-created task
horizon run my_formalization_task

# Launch a roadmap milestone (a task is inferred from it on demand)
horizon run thm_main_result

# Ad-hoc task scoped to a project (or a file)
horizon run project_a

# Ad-hoc task across multiple projects simultaneously
horizon run project_a project_b

# Run every queued task in the workspace
horizon run '*'
```

### Dry Runs and Resumption
- **Dry Run**: Preview the execution plan, target write sets, and harness configuration without launching models:
  ```bash
  horizon run my_task --dry-run
  ```
- **Resume**: Continue an interrupted or paused execution run using its run ID:
  ```bash
  horizon run --resume <run_id>     # or --resume latest
  ```

### Interactive runs

Add `--backend interactive` to hand the terminal straight to the Horizon engine — headless streaming is replaced by a live TTY, so you can type follow-up prompts and steer the agent yourself:
```bash
horizon run horizon --backend interactive
```

For a conversational, human-facing session — explain status, summarize recent runs, and make guided edits to projects/tasks/inbox/roadmap on request — use [`horizon discuss`](../../src/archon_horizon/commands/discuss.py).

Run lifecycle and resumption state are tracked in [`core/sessions.py`](../../src/archon_horizon/core/sessions.py); concurrency safety across projects is handled by [`orchestration/locks.py`](../../src/archon_horizon/orchestration/locks.py).

---

## 2. Collaboration Rounds

When a run is initiated, Horizon executes a structured loop of proof sessions and fresh-context checkpoints (implemented in [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py)):

```
[Target Task / Focus]
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ Collaboration Round (Repeated up to configured rounds) │
│                                                        │
│  1. Horizon reads task, graph, roadmap, and inbox      │
│  2. Horizon writes Lean and validates the target       │
│  3. Ground reviews strategy and workspace state        │
│  4. Horizon reconciles findings and records progress   │
└────────────────────────────────────────────────────────┘
         │
         ▼
[Final Run Report & Artifacts Emitted]
```

---

## 3. Roadmaps vs. Tasks

Horizon keeps a sharp distinction between the human's tasks and the agent-maintained roadmap:

- **Tasks (`horizon task`)**: the human's lever for launching sessions — objectives with project scope, write-set, and target files. **Human-authored only**: agents may read and `comment` (to suggest an edit) but cannot `add`/`set`/`remove` (the CLI refuses when `ARCHON_HORIZON_AGENT_ROLE` is set); to propose work an agent opens an inbox item for the human. Modeled in [`core/tasks.py`](../../src/archon_horizon/core/tasks.py), commands in [`commands/task.py`](../../src/archon_horizon/commands/task.py).
- **Roadmap (`horizon roadmap`)**: the project's **mathematical status** — the main theorems and infrastructure formalized and still to build. Horizon maintains it against the real Lean/blueprint state, while Ground checkpoints audit it from fresh context. It is a map that guides the work, **not** a work queue. Marking an item active does not launch anything and the orchestrator never turns roadmap items into tasks on its own. A human launches a milestone with `horizon run <roadmap-id>`, which **infers** a task from the item's scope on demand. Modeled in [`core/roadmap.py`](../../src/archon_horizon/core/roadmap.py), commands in [`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py).

### Common Roadmap Commands

| Command | Description |
| :--- | :--- |
| `horizon roadmap list` | List active workspace milestones and progress metrics. |
| `horizon roadmap show <id>` | Inspect specific roadmap milestones and associated items. |

---

## 4. Reports, Events & Memory

Every execution round produces persistent structured traces stored under `.archon-horizon/` (emitted via [`core/events.py`](../../src/archon_horizon/core/events.py) and persisted through the [`store/`](../../src/archon_horizon/store) layer):
- **Reports**: YAML/JSON execution reports summarizing build outputs, proof status, and tool invocations.
- **Run Logs**: Complete step-by-step audit trails of harness interactions.
- **Memory**: Persistent workspace memory items used across runs to retain lemma precedents, architectural conventions, and proven strategies.
