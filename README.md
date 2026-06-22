<div align="center">

# Archon Horizon

**Frenzymath · AI4Math**

*Workspace-first orchestration for long-horizon Lean formalization agents*

![Version](https://img.shields.io/badge/version-0.0.0-blue)
![Status](https://img.shields.io/badge/status-pre--alpha-orange)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Lean](https://img.shields.io/badge/domain-Lean%204-1f6feb)

</div>

Archon Horizon is a fresh, workspace-level successor architecture for Archon.
Instead of treating one Lean repository as the whole project, it treats a
**workspace** as the global collaboration root and each Lean codebase as a
member **project**. The goal is to coordinate blueprints, roadmaps, local/GitHub
inboxes, dashboards, and autonomous proof work across many related projects.

The core design has two abstract agents:

- **Informal agent**: maintains human-quality blueprints, DAGs, roadmaps,
  memory, reports, local issues, and dashboard material. It may use lightweight
  skills/subagents for focused work.
- **Long Horizon agent**: owns autonomous Lean formalization work for selected
  tasks. It writes Lean, runs tools/builds, repairs failures, and reports back
  through structured artifacts.

Model providers are deliberately outside the core ontology. Fable, Claude Code,
Codex, shell commands, or future systems are harnesses behind the same abstract
interfaces.

## Status

Pre-alpha, but the full backend is implemented and tested end to end:

- workspace/project config (`config.yaml`) → wired, runnable orchestrator;
- the engine seam (`Harness`) with a generic `CommandHarness` (Claude Code /
  Codex / any CLI, selected by config) and an in-process `NullHarness`;
- concrete, engine-agnostic informal and Horizon agents;
- local (filesystem), GitHub (shadow, via `gh`), and in-memory inbox providers
  with the `archon:accept` / `archon:pending` / `archon:rejected` gate;
- structured roadmap, tasks, proposals, runs, reports, events, memory stores;
- freeze enforced deterministically before dispatch; write-set locks;
- the collaboration-round driver across fixed sync boundaries;
- structural project operations (add / archive / remove / merge);
- workspace manifest git + out-of-tree project VCS wrappers;
- a blueprint LaTeX-subset parser → dependency DAG (KaTeX renders);
- roadmap markdown + static dashboard renderers;
- the `archon-horizon` CLI.

The live editable web dashboard remains a separate frontend; the static
dashboard generator and the CLI cover viewing and editing here.

See [ROADMAP.md](./ROADMAP.md) for the architecture and design rationale.

## Core Ideas

### Workspace, Not Subprojects

An Archon Horizon workspace owns the global state:

```text
config.yaml
.archon-horizon/
projects/
```

Projects are embedded directories inside the workspace, for example:

```text
projects/ag-main/
projects/topology-base/
```

The core does not use git submodules or external paths. If a project needs its
own VCS history, its git directory should live outside the project tree under
`.archon-horizon/vcs/<project>.git`.

### Asynchronous Human Interaction

Human and GitHub input enters through inbox providers. Local hints and issues
can be edited directly by the live local dashboard or CLI. GitHub issues/PRs are
shown through a GitHub-backed provider and should be accepted manually with
labels before agents use them.

Shared labels:

```text
archon:accept
archon:pending
archon:rejected
```

There is no live injection into running agents. New hints are saved immediately
and observed only at deterministic sync boundaries.

### Structured Roadmap

The roadmap should have a machine-readable source of truth:

```text
.archon-horizon/roadmap.yaml
.archon-horizon/reports/roadmap.md
```

Focused runs receive a sliced roadmap context, not a separate roadmap.

## CLI

```bash
archon-horizon init --name my-workspace          # scaffold config + .archon-horizon/
archon-horizon project add ag-main projects/ag-main --build "lake build"
archon-horizon inbox add --kind hint --body "Try the affine case first."
archon-horizon inbox list
archon-horizon run                               # all projects, workspace.rounds rounds
archon-horizon run ag-main topology-base         # focused run (scheduling restricted)
archon-horizon run --task T-0007                 # pin one task
archon-horizon run ag-main --dry-run             # plan only; do not run Horizon
archon-horizon roadmap render                    # roadmap.yaml -> reports/roadmap.md
archon-horizon dashboard                         # static HTML dashboard (read-only)
archon-horizon sync                              # refresh inbox providers (e.g. GitHub)
```

## Development

Install locally:

```bash
python -m pip install -e .
```

Run tests:

```bash
python -B -m pytest -q
```

Current repository layout:

```text
src/archon_horizon/
  agents/         concrete informal & Horizon agents (engine-agnostic)
  blueprint/      LaTeX-subset parser -> dependency DAG
  config/         config.yaml -> orchestrator; harness registry; structural ops
  core/           pure contracts (no I/O)
  harnesses/      the engine seam: Harness, CommandHarness, NullHarness
  inboxes/        local / GitHub-shadow / in-memory providers
  orchestration/  round driver, scheduler, locks, sync
  render/         roadmap markdown + static dashboard
  store/          event/task/proposal/roadmap/run/report stores + codecs
  vcs/            workspace manifest git + project VCS wrappers
  cli.py
tests/
ROADMAP.md
```

## Relationship to Archon

Archon Horizon keeps the strongest Archon ideas: blueprints, DAG-aware progress,
agent-generated reports, dashboards, harness abstraction, and Lean build
feedback. It changes the top-level shape: the workspace is the primary unit, and
projects are peers coordinated by an informal agent and a long-horizon proof
agent.

