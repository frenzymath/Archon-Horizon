# Changelog

All notable changes to Archon Horizon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, the API and on-disk formats may change between
minor releases; `horizon init --update` migrates a workspace's managed files.

## [Unreleased]

v0.1.2 work-in-progress — the "lightweight harness, finished" release. Full
rationale and measurements in
[`docs/design/v0.1.2-architecture-review.md`](./design/v0.1.2-architecture-review.md).

### Added

- **`horizon usage`** — the agent-facing consumption gauge: this session's live
  token/cost burn (streamed by the harness into `<session>/usage.json`), run
  totals, budget headroom, recent rate-limit signals, and any pause marker.
- **`workspace.budget`** (opt-in) — `session_tokens_out` cancels a live session
  that crosses it; `run_tokens_out` / `run_cost_usd` stop the run cleanly
  between sessions. A limit/budget stop writes `runs/<id>/paused.json` (reason,
  advertised retry window, focus, resume command) and `horizon run` exits with
  code 3 so relaunch loops can tell "paused, resumable" from failure.
- **Vendored semantic graph** — the dependency graph is generated from
  per-node files under `<project>/hgraph/`; agents attach comments and
  Maths/Lean reviews, and `horizon graph frontier` ranks what to prove next.
  Horizon owns the implementation and has no external graph dependency or
  secondary DAG engine.
- **`horizon ps`** — per-run process registry (`runs/<id>/process.json`):
  list live runs, reap zombie markers (`--clean`), kill a stuck run
  (`--kill <run>`); killed/crashed runs resume with `--resume`.
- **Session identity env vars** — every session now sees
  `ARCHON_HORIZON_RUN/SESSION/SESSION_DIR/ROUND/ROUNDS/TASK/TASK_TITLE/PROJECTS`
  (documented in the `horizon` skill), so any engine can read its own context
  without prompt-parsing.
- **Engine-native orientation** — `init` writes `CLAUDE.md`/`AGENTS.md`
  pointers to the `horizon` skill, so any engine launched in the workspace
  self-orients without pushed prompt prose.
- Dashboard: hgraph-style little-squares mini-map + segmented progress bars on
  the Blueprint table of contents; `/api/blueprints` split out of `/api/state`.

### Changed

- **The automated prompt is now a task directive + "load the `horizon` skill".**
  All pushed role prose/policy/workspace state is gone; the skill is the
  (user-editable) contract and state is pulled through the CLI. The old
  `.archon-horizon/prompts/` overrides are inert.
- **Dashboard hot path** — measured on a 162-run workspace, a poll cost ~12 s
  of server work every 5 s; it is now ~85 ms when idle (ETag short-circuit
  before compute) and ~0.5 s when changed (session-state cache persisted under
  `.archon-horizon/cache/`, subagent materialization moved to the write path,
  incremental events parse, subtree-stamp collection caches).
- Failure classification reads the engine's typed error events before grepping
  stderr; an advertised "retry after Xs" stretches the retry backoff.
- `horizon run` has one launch path (`--supervisor`, `horizon run horizon`, and
  focused runs all reduce to a focus + RunRecord).

### Removed

- Dead config from the two-agent era (`roles`, `start_with`, `end_with`,
  `ground_subagents`, `subagent_harness`, `references.transcription`,
  `scheduler.unknown_write_set_policy`) — old keys are ignored, not errors.
  The `workspace.ground_agent` block is now fully gone too: `discuss`, the
  post-init advisor, and `horizon subagent` all use the horizon harness.
- The aggregate per-session "Changes" view (`/api/run/changes`,
  `/api/run/working-changes` and their UI panel): progress is read from the
  commit-granular git view (commit cards + per-commit file diffs), which is
  the durable record anyway.
- The former embedded DAG package, parser fallback, and standalone DAG command;
  `horizon graph` is the single graph surface.
- The vestigial cross-process commit-lock stack, `project_checkpoint`,
  `ProjectGit`, git-notes helpers; the old graph exporters/queries/reporter;
  the in-memory inbox provider; prompt composition and its templates;
  write-domain enforcement (`core/permissions.py`); the legacy Python-rendered
  dashboard fallback (a no-dist install now gets build instructions; the JSON
  API stays live).

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
- **Blueprint → DAG.** A LaTeX-subset blueprint parser building dependency
  graphs for Lean/blueprint queries and dashboard rendering.
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
  `task`, `project`, `skills`, `blueprint`, graph queries, `search`, `sync`,
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
