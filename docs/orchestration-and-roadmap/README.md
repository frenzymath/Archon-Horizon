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

```bash
# Run a specific task
horizon run my_formalization_task

# Run all tasks induced by a specific project
horizon run project_a

# Run across multiple projects simultaneously
horizon run project_a project_b

# Run across all projects in the entire workspace
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

### Single-role and interactive runs

Besides the full alternation, you can run **one role** on its own:
```bash
horizon run ground     # a single Ground planning session (no alternation)
horizon run horizon    # a single Horizon prover session over the current focus
```

Add `--backend interactive` to a role to hand the terminal straight to the engine — headless streaming is replaced by a live TTY, so you can type follow-up prompts and steer the agent yourself:
```bash
horizon run horizon --backend interactive
```

For a purely conversational, human-facing session — explain status, summarize recent runs, and make guided edits to projects/tasks/inbox/roadmap on request — use [`horizon discuss`](../../src/archon_horizon/commands/discuss.py) (a Ground-flavored companion that only modifies things when you ask).

Run lifecycle and resumption state are tracked in [`core/sessions.py`](../../src/archon_horizon/core/sessions.py); concurrency safety across projects is handled by [`orchestration/locks.py`](../../src/archon_horizon/orchestration/locks.py).

---

## 2. Collaboration Rounds

When a run is initiated, Horizon executes a structured **collaboration loop** between the Ground and Horizon agents (implemented in [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py)):

```
[Target Task / Focus]
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ Collaboration Round (Repeated up to configured rounds) │
│                                                        │
│  1. Ground Agent analyzes blueprint & roadmap state    │
│  2. Target theorems & write sets dispatched to Horizon │
│  3. Horizon Agent writes Lean & runs `lake build`      │
│  4. Proof repairs & results emitted as structured JSON │
└────────────────────────────────────────────────────────┘
         │
         ▼
[Final Run Report & Artifacts Emitted]
```

---

## 3. Roadmaps vs. Tasks

Horizon makes a sharp distinction between human-defined tasks and dynamic agent roadmaps:

- **Tasks (`horizon task`)**: High-level, human-managed objectives defining project scopes, permissions, directives, and target files. Modeled in [`core/tasks.py`](../../src/archon_horizon/core/tasks.py), commands in [`commands/task.py`](../../src/archon_horizon/commands/task.py).
- **Roadmap (`horizon roadmap`)**: Dynamic milestones and granular work items maintained by the Ground agent to organize pending proof steps, blocker dependencies, and completion estimates. Modeled in [`core/roadmap.py`](../../src/archon_horizon/core/roadmap.py), commands in [`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py).

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
