# Changelog

All notable changes to Archon Horizon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, the API and on-disk formats may change between
minor releases; `horizon init --update` migrates a workspace's managed files.

## [Unreleased]

## [0.1.0] — 2026-06

First public release. Archon Horizon is a workspace-first successor architecture
to [Archon](https://github.com/frenzymath/Archon): a **workspace** is the global
collaboration root and each Lean codebase is a member **project**, coordinated by
a **Ground agent** (blueprints, DAG, roadmap, reports, inboxes) and a
**long-Horizon agent** (autonomous Lean formalization).

### Added

- **Workspace model.** `config.yaml` + a tracked `.archon-horizon/` state dir.
  Projects are embedded directories with their own build and (optional)
  out-of-tree git under `.archon-horizon/vcs/<project>.git` — no submodules.
- **Two abstract agents.** Engine-agnostic Ground and Horizon agents with a
  collaboration-round driver across deterministic sync boundaries.
- **Harness seam.** A `Harness` interface with a generic `CommandHarness`
  (Claude Code, Codex, or any CLI, selected by config), a `codex` harness, and
  an in-process `NullHarness`. Claude Code backends: `default`, `vscode`,
  `desktop`, and the optional `claude-p` headless backend. Provider routing for
  Kimi/Moonshot and DeepSeek (direct keys or OpenRouter fallback).
- **Inboxes.** Local (filesystem), GitHub (shadow via `gh`), in-memory, and
  sharded providers, gated by `archon:accept` / `archon:pending` /
  `archon:rejected` labels. No live injection — hints are observed at sync
  boundaries only.
- **Roadmap, tasks, runs, reports, events, memory** stores with YAML/JSON
  codecs; tasks are human-only, agents organize pending work via the roadmap.
- **Blueprint → DAG.** A LaTeX-subset blueprint parser building a dependency
  DAG, plus the vendored **leandag** engine for Lean/blueprint graph queries and
  effort estimates (KaTeX renders the graph).
- **Permissions & freeze.** Deterministic freeze enforcement before dispatch and
  write-set locks; protections stored as persistent inbox items.
- **Native subagents.** Read-only starter descriptors (blueprint-reviewer,
  diff-auditor) compiled per-engine into workspace-local `.claude/agents` /
  `.codex/agents`; skills installed under `.claude/skills/`.
- **Dashboards.** A live editable web dashboard (separate frontend) and a static
  HTML snapshot generator.
- **Workspace search** (`horizon search`) over mathlib, workspace projects, and
  configured `external_libraries`.
- **CLI** (`horizon`): `init`, `setup`, `update`, `run`, `inbox`, `roadmap`,
  `task`, `project`, `skills`, `blueprint`, `leandag`, `search`, `sync`,
  `dashboard`. Every command supports `--json` (pure JSON on stdout, human chrome
  on stderr).
- **Version stamping.** `horizon init` records the Horizon version in
  `.archon-horizon/version`; workspace commands warn on drift and point at
  `horizon init --update` (refresh a stale workspace) or `horizon update`
  (upgrade a stale tool).
- **Install / update.** `curl … | bash` installer (`install.sh`) and a
  `horizon update` self-update command.

[Unreleased]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/frenzymath/Archon-Horizon/releases/tag/v0.1.0
