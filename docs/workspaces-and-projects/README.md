# Workspaces & Projects Guide

Archon Horizon treats the **workspace** as the primary unit of management and collaboration. A single workspace encapsulates configuration, multi-project version control tracking, permissions, and metadata. The workspace model lives in [`core/workspace.py`](../../src/archon_horizon/core/workspace.py), and its configuration schema in [`config/schema.py`](../../src/archon_horizon/config/schema.py).

---

## Table of Contents

- [1. Workspace Directory Layout](#1-workspace-directory-layout)
- [2. Initializing a Workspace](#2-initializing-a-workspace)
- [3. Managing Projects](#3-managing-projects)
  - [Common Project Commands](#common-project-commands)
- [4. Permissions & Freeze Enforcement](#4-permissions--freeze-enforcement)

---

## 1. Workspace Directory Layout

When you initialize an Archon Horizon workspace, the framework scaffolds a central configuration file and a hidden state directory:

```
my-horizon-workspace/
├── config.yaml                   # Global workspace defaults, models, and harness settings
├── project_a/                    # Embedded Lean 4 project codebase
├── project_b/                    # Another member project
└── .archon-horizon/              # Managed state and orchestration data
    ├── version                   # Stamped Horizon tool version
    ├── vcs/                      # Out-of-tree Git repositories for member projects
    ├── roadmap/                  # Milestones and task breakdowns
    ├── inboxes/                  # Local filesystem inbox items
    ├── reports/                  # Structured agent execution reports
    └── search/                   # Cached offline declaration search indices
```

`config.yaml` is parsed and validated by [`config/loader.py`](../../src/archon_horizon/config/loader.py) against the schema in [`config/schema.py`](../../src/archon_horizon/config/schema.py).

---

## 2. Initializing a Workspace

To create a new workspace or scaffold Horizon within an existing directory (see [`commands/init.py`](../../src/archon_horizon/commands/init.py)):

```bash
horizon init --root ./my-workspace
```

During initialization, Horizon will prompt you to set default engine parameters in `config.yaml`, add initial projects, and optionally spawn an agent to review your setup and assist with project structure.

> [!NOTE]
> You can run `horizon init` non-interactively or invoke `horizon init --update` inside an existing workspace to refresh managed files (such as subagents, skills, and MCP configurations) after upgrading the CLI.

---

## 3. Managing Projects

Projects represent individual Lean 4 libraries or packages. Horizon supports two integration styles:

1. **Embedded Subdirectories**: Traditional directories located directly inside the workspace root (e.g., `./my-project/`).
2. **Out-of-Tree VCS Tracking**: For repositories that should remain isolated without introducing Git submodules, Horizon manages dedicated checkouts inside `.archon-horizon/vcs/<project>.git`.

Project registration and listing are implemented in [`commands/project.py`](../../src/archon_horizon/commands/project.py); config mutations go through [`config/operations.py`](../../src/archon_horizon/config/operations.py).

### Autogit (out-of-tree history)

Horizon journals its own work **without ever creating a `.git` at the workspace or project root** — see [`vcs/git.py`](../../src/archon_horizon/vcs/git.py). Each repo is a bare git directory driven via explicit `--git-dir`/`--work-tree`: the workspace at `.archon-horizon/vcs/workspace.git`, each VCS-enabled project at `.archon-horizon/vcs/<project>.git`. Your own `<root>/.git`, if any, is untouched.

- **Default branch is `main`** (`git init --bare --initial-branch=main`, with a `symbolic-ref` fallback for git < 2.28). It's set **only at creation** — if you manually switch a repo to another branch, autogit keeps committing onto *your* branch; nothing checks out, resets, or pushes.
- **Excludes live in the git dir's `info/exclude`, not a `.gitignore`** (refreshed on every init, so existing workspaces self-heal). They keep Lean/build artifacts (`.lake/`, `*.olean`, `lake-packages/`), caches, and secrets out; project files are added *without* `-f`, so binaries never sneak in.
- **Commits carry rich metadata**: `Run/Round/Role/Session/Task` plus per-project SHAs, authored as the acting agent (`Archon Horizon (Ground|Horizon)`) but committed by the system identity. A `pre-commit` hook blocks obvious secrets (`ARCHON_HORIZON_ALLOW_SECRETS=1` to override).

### Common Project Commands

| Command | Description |
| :--- | :--- |
| `horizon project add <name>` | Register a new or existing Lean project into the workspace. |
| `horizon project list` | Display all configured projects along with their status and paths. |
| `horizon sync` | Import inbox provider shadows (e.g. GitHub issues) into the local inbox. |

---

## 4. Permissions & Freeze Enforcement

Before dispatching autonomous formalization tasks to the Horizon agent, the workspace enforces **deterministic freeze checks** and write-set locks (see [`core/freeze.py`](../../src/archon_horizon/core/freeze.py) and [`core/permissions.py`](../../src/archon_horizon/core/permissions.py)):
- **Write Sets**: Tasks explicitly define which projects or files the agent is authorized to modify.
- **Pre-Dispatch Freeze**: Horizon verifies clean Git boundaries and ensures that unapproved modifications do not leak across project seams during execution rounds.

Standing protections that soft-freeze specific declarations are covered in the [Inboxes & Communication guide](../inboxes-and-communication/README.md).
