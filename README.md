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

This repository is currently a pre-alpha implementation skeleton. The first
contracts are in place for:

- workspace/project configuration;
- local and GitHub-like inbox providers;
- `archon:accept`, `archon:pending`, and `archon:rejected` labels;
- structured roadmaps, tasks, proposals, events, memory, and freeze rules;
- abstract informal/Horizon agents;
- abstract harnesses;
- deterministic sync boundaries between agents;
- a smoke-tested orchestration loop.

See [ROADMAP.md](./ROADMAP.md) for the architecture and open design direction.

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

## Planned CLI Shape

The intended user-facing entry point is:

```bash
archon-horizon run
```

Focused runs restrict scheduling to selected projects:

```bash
archon-horizon run ag-main topology-base
```

Narrower task/proposal entry points may be added later:

```bash
archon-horizon run --task T-0007
archon-horizon run --proposal P-0003
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
  agents/
  config/
  core/
  harnesses/
  inboxes/
  orchestration/
  store/
tests/
ROADMAP.md
```

## Relationship to Archon

Archon Horizon keeps the strongest Archon ideas: blueprints, DAG-aware progress,
agent-generated reports, dashboards, harness abstraction, and Lean build
feedback. It changes the top-level shape: the workspace is the primary unit, and
projects are peers coordinated by an informal agent and a long-horizon proof
agent.

