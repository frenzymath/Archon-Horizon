# Changelog

All notable changes to Archon Horizon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, the API and on-disk formats may change between
minor releases; `horizon init --update` migrates a workspace's managed files.

## [Unreleased]

- Skills now treat a **missing or stub blueprint** like an empty roadmap:
  unfinished orientation. Horizon should author a complete source-facing
  chapter (statements, proofs, `\uses`, cites) before or with the first
  Lean, and keep `.tex` the live route as strategy changes.

## [0.1.5] — 2026-09-04

Formalization-quality skills and reviewers, a heartbeat benchmark, complete
roadmap CLI mutations, and workspace-local session scratch.

### Added

- **Formalization quality program (AJCR lessons)** — blueprint-first complete
  proofs with bibliography/`\dcref`/`\source` and explicit adaptation notes;
  mathlib naming/docstrings/tree layout with leanprover-community guide links;
  writable `janitor` for Lean/blueprint layout moves; `restart-module` skill for
  backup-and-rewrite when layered debt or fix-loops dominate; lean-quality and
  API composition guidance treat `set_option` heartbeats as blockers. Corpus
  guidance lives in [`docs/design/formalization-review.md`](./design/formalization-review.md).
- **`definition-quality` skill + `definition-quality-reviewer`** — Mathlib-based
  criteria for good/bad definitions; detect suspects from how theorems/lemmas
  consume them (shared consumer pain → definition root cause).
- **`horizon benchmark`** (+ dashboard **Benchmark** view / `/api/benchmark`) —
  rank Lean files by summed `set_option` heartbeat budgets so agents can find
  costly modules.
- **Roadmap CLI completeness** — `horizon roadmap show`, `rename`, fuller `set`
  (`--project`, `--depends-on`, `--inbox-ref`, `--task-ref`), and `remove
  --cascade` (default remove un-nests children and drops depends-on edges).
  Skills (`horizon`, `horizon-start`, `strategy-convergence`) and the janitor
  descriptor now treat an empty or frozen roadmap as unfinished strategy: agents
  should add/move/delete/rename items as the long-term plan changes, not only
  flip status on pre-seeded rows.
- Focused advisory skills for review method, progress integrity, source
  fidelity, semantic adversarial checks, blueprint integrity, proof load-bearing,
  consumer dependencies, API composition, Lean quality, Mathlib orientation,
  verification evidence, graph traceability, provenance isolation, strategy
  convergence, run health, external boundaries, transcription fidelity, release
  reproducibility, and review adjudication.
- **`honesty` skill and read-only `honesty-reviewer`** — certificate taxonomy
  (proved/conditional/imported/axiom-backed/empty/unverified),
  hypothesis-packaging and vacuity probes, stale-evidence checks, and a
  source-frontier test for repeated wrapper/re-expression churn.
- **`source-discovery`** — a channel and provenance map for local references,
  GitHub repository/review history, Mathlib source and PRs, Tau Ceti artifacts,
  and Zulip discussions, including credential and access-boundary guidance.
- Separate read-only reviewer descriptors for those lanes. The roster remains
  optional: no automatic review stage or dispatch gate was added.
- Workspace-local, per-session scratch under `.archon-horizon/tmp/`: agent
  subprocesses receive `TMPDIR`/`TMP`/`TEMP` there, successful sessions reclaim
  their scratch automatically, and `horizon tmp clean --apply` safely removes
  stale leftovers while protecting live runs.

### Changed

- **`work-reviewer`** stays narrowly focused on honest progress, anti-loop
  checks, and task/report/diff consistency; focused reviewers own the other
  lanes.
- **Ground** is an optional workspace-wide perspective the lead may choose, not
  a scheduled second orchestrator.
- **Logs run/session task chip** — the task link sits in the same meta-chip row
  as time/usage (not as a separate trailing control); long task ids ellipsize
  so they no longer shove the other tags around.
- Stale-skill fallback prompt is bounded and no longer mandates helper
  dispatch; headless Horizon asks for `honesty-reviewer` before new
  certificate/`\leanok` claims or repeated frontier moves.

### Fixed

- **Blueprint textbook chapters** are served from `content.tex` the same way
  hgraph does (include only reached chapters, strip preamble definitions,
  harvest KaTeX macros from the whole tree).
- Published DAG caches still using `kind`/`content_type` are filtered to
  countable nodes so stale remarks cannot affect graph status.

## [0.1.4] — 2026-08-28

### Added

- **`horizon ledger status` / `horizon ledger prune [--gc]`** — inspect and
  retro-clean agent ledgers that still track Horizon state or generated hgraph
  trees (index-only remove; working tree untouched; optional `git gc` to reclaim
  pack space).
- **`lean-isolator` subagent descriptor** for isolating large Lean declarations
  that time out or fail to produce an `.olean`.
- Fixed English **date/time helpers** for the dashboard (`formatChipDateTime`, …).

### Changed

- **Agent ledger is source-only.** The out-of-tree workspace ledger
  (`.archon-horizon/vcs/workspace.git`) records Lean, blueprints, and
  `config.yaml` only. The entire Horizon control-plane tree (`.archon-horizon/`)
  and all `**/hgraph/` paths are excluded; they stay on disk for the live
  dashboard. Users version what they want in their own root `.git`. Session
  integration commits no longer stage inbox/tasks/runs/roadmap state.
- **Session integration** commits only `config.yaml` + scoped project sources.
- **Agent dirty-check / commit reminders** ignore non-source ledger noise
  (state tree and hgraph).
- **Static dashboard export** prefers the user root `.git` when present; ledger
  is not the publish path for Pages snapshots.
- **CLI entry** routes through `archon_horizon.__main__:main` so
  `agent-hook-fast` can bypass the full Typer import on every tool boundary.
- **Run/session chips** show date and time (`Aug 28, 15:20`), not time alone.

### Fixed

- **Session model chip no longer inherits the first subagent's model.**
  `observed_model` and the dashboard skip nested-subagent events, so a parent
  that ran `gpt-5.6-sol` is not labeled with a child model (e.g. luna).
- **`horizon init` model prompt is harness-family-aware.** Choosing Codex clears
  a leftover Claude default (opus/sonnet/…) and shows Codex-only help; the reverse
  for Claude Code. Pre-existing `CLAUDE.md` / `AGENTS.md` / `.env` are respected
  and announced.
- **Dashboard dates stay English** (`en-US`) so a French OS locale no longer
  yields localized month names in the UI.
- **Inbox order uses real latest activity** (item timestamps, comments, history).
  Collapsed cards show an activity date chip.
- **hgraph Lean extraction**: `/--` doc comments only; empty-body guard so
  declarations are not written with empty node bodies.
- **Secret guard** re-stages with `git add -f` after redaction so ignored paths
  still get the scrubbed blob.
- **Static export / dashboard**: bound history export, stamp export time, keep
  expanded chapter graphs intra-chapter only (also carried from the v0.1.3
  follow-ups on this branch).

## [0.1.3] — 2026-08-18

### Added

- Live Codex rollout telemetry now discovers native and nested subagents from
  spawn provenance while the parent is running, records their model/role/depth
  and terminal status, and surfaces context compactions separately from current
  request and cumulative token counters.
- `horizon attempt save` preserves rejected drafts and diagnostics as explicit
  session artifacts; `horizon check` serializes resource-heavy Lean checks,
  coalesces identical concurrent requests, enforces timeouts, and records results.

### Changed

- Session change panels distinguish agent and integration commits, rejected
  attempts, and recorded checks. Long sessions get elapsed-time progress/commit
  checkpoints, including task/roadmap/inbox writes.
- The transcript keeps only the newest Codex context snapshot visible while the
  append-only log retains every raw telemetry event. Current-request, cached,
  cumulative, context-window, and compaction values remain in session details.
- Live dashboard state derives large changing transcripts from a bounded tail and
  only rematerializes subagents when appended rows contain child activity; the
  latest state-generation timing is available at `/api/performance`.

### Fixed

- Headless Codex runs now reconstruct native and nested subagents from rollout
  spawn provenance, stream their activity before the parent exits, suppress
  duplicate reconciliation events, and distinguish completed, failed,
  interrupted, cancelled, and orphaned children.
- Both Codex compaction record shapes are normalized without conflating current
  request input, cached input, cumulative usage, or the model context window.

## [0.1.2] — 2026-07

The "lightweight harness, finished" release, extended with multi-team
collaboration on a shared workspace. Full rationale and measurements in
[`docs/design/v0.1.2-architecture-review.md`](./design/v0.1.2-architecture-review.md).

### Added

- **Enforced freeze CLI** — `horizon freeze add/list/remove` maintains
  config-backed agent/project/file/declaration/blueprint-node rules; semantic
  `inbox protect` constraints remain clearly separate.
- **Release version helper** — `scripts/version.py` updates and checks the Python
  package, README badge, dashboard metadata, and demo workspace stamp together.

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
- **Teams collaborating on one workspace** — each `horizon run` is a team;
  parallel teams coordinate through shared state, not synchronous meetings.
  Inbox items are now either shared (default) or **owned by one task** (a private
  per-team inbox); **read-state is per-team** (`horizon inbox read`/`unread`,
  `inbox list --mine/--unread/--task <id>`); and teams can **direct-message** one
  another with `inbox add --to task:<id>` / `run:<id>`.
- **Roadmap as a project board** — roadmap items carry `owner`, a `milestone`
  label (grouping/filtering, no due dates), and pinned commits
  (`horizon roadmap set/add --owner --milestone`, `set --pin-commit/--unpin-commit`,
  `list --milestone/--owner`); `horizon task set --roadmap-ref/--inbox-ref` links a
  task to its milestone/inbox. A milestone-grouped **`/board`** dashboard view and
  **clickable local-reference chips** (roadmap/task/inbox/node/commit ids in any
  rendered text) surface it in the UI; new `GET /api/commit?sha=` resolves a SHA.
- **`horizon permissions`** + **`workspace.delegation`** (default deny) — the
  standing consent an agent reads before launching work for *other* teams (new
  tasks, or a whole new `horizon run`); spawning subagent workers *within* a team
  needs no permission.
- **Pre-command synchronizer** — inside a session, every `horizon` command first
  prints a cached, stderr-only digest (unread inbox for the task, session
  runtime/tokens, other live runs) so an agent stays aware without polling;
  `ARCHON_HORIZON_NO_SYNC=1` disables it and it never touches `--json` stdout.
- **Provenance-defaulted flags** — `--author`, `--project`, inbox owner/reader,
  and task default from the session's `ARCHON_HORIZON_*` env, so an agent rarely
  passes them.

### Changed

- Agent startup names the horizon skill by absolute path, agent-authored state
  and hgraph comments retain run/session/task provenance, inbox triage defaults
  to open items, and the session contract requires inbox/roadmap maintenance.
- Formalization notes now belong on hgraph nodes rather than in mathematical
  blueprint `.tex` sources.

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
- **The ledger secret guard now redacts instead of blocking.** A high-confidence,
  key-prefixed, case-sensitive credential match is replaced with `XXXX` in the
  staged content (and re-staged) with a warning to rotate it — the commit is never
  blocked, and the scan fails open so a guard bug can't wedge commits. Being
  case-sensitive, long camelCase identifiers no longer false-match.
  `ARCHON_HORIZON_ALLOW_SECRETS=1` skips it; the separate silent-clobber/deletion
  guard is unchanged.
- **hgraph Lean declaration extraction** now covers `structure`/`inductive`/`class`
  (not just `theorem`/`lemma`/`def`/`abbrev`/`instance`), allows Unicode/subscript
  declaration names, and honours the `_root_.` escape (`theorem _root_.Foo.bar`
  resolves to `Foo.bar`), so more `\lean{}` references resolve instead of going stale.
- **`[temporary]` inbox items auto-archive** at run finish once they have been
  visible for a full run (consumed one-shot notes); items created during the run
  are kept for the next one.
- Concurrent `horizon inbox add` calls get **distinct ids** (flock-serialised id
  allocation, fail-open). The UI's per-session commit log is **latest-first**, and
  the automated session prompt leads with the **absolute** skill path so a
  non-interactive engine never guesses a failing relative `.claude/skills/...` path.

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

[Unreleased]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.2...v0.1.3
[0.1.0]: https://github.com/frenzymath/Archon-Horizon/releases/tag/v0.1.0
