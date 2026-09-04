# Orchestration & Roadmap Guide

Archon Horizon manages complex mathematical formalization through structured orchestration, coordinating autonomous proof work via explicit tasks, multi-round collaboration loops, and persistent roadmaps. The loop itself is driven by [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py) and [`orchestration/scheduler.py`](../../src/archon_horizon/orchestration/scheduler.py).

---

## Table of Contents

- [1. Running Autoformalization Tasks](#1-running-autoformalization-tasks)
  - [Dry Runs and Resumption](#dry-runs-and-resumption)
- [2. Collaboration Rounds](#2-collaboration-rounds)
- [3. Roadmaps vs. Tasks](#3-roadmaps-vs-tasks)
  - [The Roadmap as a Project Board](#the-roadmap-as-a-project-board)
  - [Common Roadmap Commands](#common-roadmap-commands)
  - [Linking Tasks to Roadmap & Inbox](#linking-tasks-to-roadmap--inbox)
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

When a run is initiated, Horizon executes a structured loop of proof sessions;
the lead agent may add a fresh-context helper when the scope benefits from one
(implemented in [`orchestration/orchestrator.py`](../../src/archon_horizon/orchestration/orchestrator.py)):

```
[Target Task / Focus]
         │
         ▼
┌────────────────────────────────────────────────────────┐
│ Collaboration Round (Repeated up to configured rounds) │
│                                                        │
│  1. Horizon reads task, graph, roadmap, and inbox      │
│  2. Horizon writes Lean and validates the target       │
│  3. Horizon may choose a scoped review/helper          │
│  4. Horizon reconciles findings and records progress   │
└────────────────────────────────────────────────────────┘
         │
         ▼
[Final Run Report & Artifacts Emitted]
```

---

## 3. Roadmaps vs. Tasks

Horizon keeps a sharp distinction between the human's tasks and the agent-maintained roadmap:

- **Tasks (`horizon task`)**: the human's lever for launching sessions — objectives with project scope, write-set, and target files. Open to agents and humans alike: both may `add`, `set` (including status and roadmap/inbox refs), `comment`, and `remove`. The machine (scheduler/orchestrator) only ever writes `queued`/`running`; every terminal status (`done`/`blocked`/`failed`) is the agent's own word, and an agent declares `done` only when the work is *fully* complete. Modeled in [`core/tasks.py`](../../src/archon_horizon/core/tasks.py), commands in [`commands/task.py`](../../src/archon_horizon/commands/task.py).
- **Roadmap (`horizon roadmap`)**: the project's **mathematical status** — the main theorems and infrastructure formalized and still to build. Horizon maintains it against the real Lean/blueprint state, and Ground may audit it from fresh context when the lead agent chooses that perspective. It is a map that guides the work, **not** a work queue. Marking an item active does not launch anything and the orchestrator never turns roadmap items into tasks on its own. A human launches a milestone with `horizon run <roadmap-id>`, which **infers** a task from the item's scope on demand. Modeled in [`core/roadmap.py`](../../src/archon_horizon/core/roadmap.py), commands in [`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py).

### The Roadmap as a Project Board

Beyond its status and hierarchy, each roadmap item carries lightweight board metadata (backed by [`core/roadmap.py`](../../src/archon_horizon/core/roadmap.py) — `item_owner`/`item_milestone`/`item_pinned_commits`, folded in by `_apply_board_meta` in [`commands/roadmap.py`](../../src/archon_horizon/commands/roadmap.py)):

- **`owner`** — the team or agent responsible for the item, a categorization aid rather than an assignment lock. Who is *currently* running an item is **derived live** from running tasks' `roadmap_refs`, never stored on the item itself.
- **`milestone`** — a free string label for grouping and filtering. It is **not** a due date and carries no schedule; it simply buckets related items together.
- **`pinned_commits`** — commit SHAs pinned to the item as concrete deliverables (newest first).

Set these when adding or editing an item:

```bash
# Add an item already scoped to an owner and milestone
horizon roadmap add --id A.4 --title "..." --owner algebra-team --milestone "phase-1"

# Edit board fields on an existing item; pin/unpin deliverable commits
horizon roadmap set A.4 --owner algebra-team --milestone "phase-1" \
    --pin-commit <sha> --unpin-commit <sha>

# Filter the outline by milestone or owner
horizon roadmap list --milestone "phase-1"
horizon roadmap list --owner algebra-team
```

The same board also surfaces in the web dashboard as a milestone-grouped [`/board` view](../dashboard-and-search/README.md), where items with a live running task pulse as "live".

### Common Roadmap Commands

| Command | Description |
| :--- | :--- |
| `horizon roadmap list` | List roadmap items as an indented outline; filter with `--milestone <label>` / `--owner <t>`. |
| `horizon roadmap show <id>` | Inspect a specific roadmap item (fields, hierarchy, board metadata, subtree progress). |
| `horizon roadmap add --id <id> --title <t>` | Add an item; `--project` defaults from the session's `ARCHON_HORIZON_PROJECTS` when omitted. Optional `--parent`, `--depends-on`, `--owner`, `--milestone`. |
| `horizon roadmap set <id> …` | Update any field: status/title/summary/kind/priority, nest with `--parent`/`--depth`, board metadata, `--project`, `--depends-on`, `--inbox-ref`/`--task-ref`, `--pin-commit`/`--unpin-commit`. Empty string clears optional fields. |
| `horizon roadmap rename <old> <new>` | Rename an item id and rewrite parent/depends-on links that pointed at it. |
| `horizon roadmap remove <id> [--cascade]` | Delete an item; children un-nest by default, or delete with `--cascade`. |
| `horizon roadmap comment <id> --body …` | Add a concise progress comment on a milestone. |

Agents are expected to **build and reshape** this outline as strategy, not only flip status on pre-seeded rows: an empty roadmap on a multi-session formalization task is unfinished orientation. The same applies to the **blueprint**: a missing or stub chapter is unfinished orientation — write a complete mathematical route and keep `.tex` aligned with Lean as the route changes (see [`blueprint-conventions`](../../src/archon_horizon/skills/blueprint-conventions/SKILL.md)).

### Linking Tasks to Roadmap & Inbox

A task records which roadmap items and inbox items it advances via its `roadmap_refs`/`inbox_refs`. Set or repair these through the safe CLI (see [`commands/task.py`](../../src/archon_horizon/commands/task.py) — `set_task`):

```bash
horizon task set <id> --roadmap-ref <rid> --inbox-ref <iid>   # both repeatable
```

Both flags are repeatable and replace the existing refs. These links are what let the board derive its "live" status and what drives roadmap status-sync when a linked task reaches a terminal state. Like `roadmap add`, `horizon task add` defaults `--project` from the session's `ARCHON_HORIZON_PROJECTS` when omitted (`provenance_project` in [`commands/shared.py`](../../src/archon_horizon/commands/shared.py)).

---

## 4. Reports, Events & Memory

Every execution round produces persistent structured traces stored under `.archon-horizon/` (emitted via [`core/events.py`](../../src/archon_horizon/core/events.py) and persisted through the [`store/`](../../src/archon_horizon/store) layer):
- **Reports**: YAML/JSON execution reports summarizing build outputs, proof status, and tool invocations.
- **Run Logs**: Complete step-by-step audit trails of harness interactions.
- **Memory**: Persistent workspace memory items used across runs to retain lemma precedents, architectural conventions, and proven strategies.
