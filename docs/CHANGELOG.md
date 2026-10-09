# Changelog

All notable changes to Archon Horizon are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is `0`, the API and on-disk formats may change between
minor releases. See the [pipeline setup guide](pipeline-setup.md) for current
service configuration and catalog updates.

## [Unreleased]

- The public dashboard demo uses a synthetic administrator with populated
  execution hosts, provider harnesses, reviewers, skills, prompts and subagent
  descriptors. Forge and Zulip tabs show local read-only previews without live
  credentials or external service connections.

## [0.2.0-alpha.2] — 2026-10-09

- Align Python, frontend and README version metadata with the second alpha
  snapshot. Preserve `v0.2.0-alpha.1` and add the historical `v0.1.5` release tag.

- Graph milestones use a star shape, including historical `type: milestone`
  nodes. The Nodes directory combines milestone, type, progress, and text filters
  with accurate pagination; custom node types are discovered from project sources.

- References now store PDFs, original TeX, archives, and companion documents in
  durable project artifacts. The dashboard supports upload, preview, download,
  and archive; dispatched agents can list, upload, and retrieve files with
  checksum verification and durable transfer recovery. Apply migration
  `0031_reference_files` before restarting an existing control plane.

### Pipeline redesign

- `horizon`, `horizon-pipeline`, and `python -m archon_horizon` now enter the
  same PostgreSQL control plane. The former SQLite control plane, its CLI
  commands, and `/api/v2` endpoints are removed; the current API is `/api/v3`
  and the dashboard is served at `/pipeline`.
- Existing 0.1.5 data is not converted by the new Alembic migrations. Prepare
  a separate installation and PostgreSQL database, review its configuration,
  then run the explicit `migrate` command. See [setup](pipeline-setup.md).
- Missions now form a revision-checked ownership tree, while mathematical
  dependencies remain in the roadmap graph. Runs use durable assignments,
  retained obligations and provider context, leased executions, and a root
  maintainer that closes each phase. Preprocessing, formalization, and
  postprocessing share the same scheduling and review machinery.
- Workers journal uncertain API requests and Git publications for recovery.
  The dashboard provides phase-aware projects, missions, activity, roadmap
  progress, administration, and Forgejo/Zulip integrations. Installation
  extras separate control-plane, worker, client, and search dependencies.
- Release wheels include the built dashboard, grouped skills, reviewer and
  specialist descriptions, and migration history. Source distributions retain
  the frontend build inputs and development checks.

### Earlier development notes

The notes below record intermediate work since 0.1.5. Some describe the
superseded SQLite platform or `/api/v2` behavior; the pipeline redesign above
and the current guides define the shipped interfaces.

- Host storage admission measures 10 GiB / 10% headroom against the user or
  group quota when one is set, not against the larger tmpfs or backing
  filesystem. Quota-limited `/tmp` with remaining space no longer marks the
  host unavailable or blocks new workers.

- Maintainer review treats a `formally_proved` parent whose `children` are
  still informal as stale graph: cascade bounded prerequisite updates into
  the same PR rather than merging an inverted DAG.

- Workspace and graph Forgejo issues are worker-accessible
  (`GET`/`POST`/`PATCH /api/v2/forge/issues`). SessionStart briefings list
  open tickets so collaborators and maintainers can raise a lease before a
  larger change and close completed hygiene without a periodic scan. Zulip
  remains live discussion; theorem gaps stay on the graph.

- Packaged `horizon-efficiency` skill: filter context with `ls`/`rg`/`git
  log` instead of dumping threads or trees; use LSP before Lake; skip
  re-checking already labeled or extra-library evidence; do not weaken
  hypotheses for short-term progress; treat Zulip as targeted `@` mentions
  (humans via `@**Name**`, workers via `[@label](...horizon-mention=1)`),
  not a session log. Collaborator and maintainer profiles load it with
  `horizon-start`.

- Project-library PRs are legacy. Skills and prompts no longer ask agents to
  create or review `library-{project}` contributions; Lean is published in the
  shared workspace. Startup no longer auto-provisions a library repository,
  `POST /api/v2/projects/{project}/library` refuses new creation, and
  briefings/maintenance no longer scan or count library PRs. SessionStart hooks
  and the Activity dashboard rewrite leftover launch snapshots that still said
  `N Library PRs open` / `N awaiting review` onto the live DAG counts. The
  SessionStart briefing is injected once at a cold session start; resume,
  compact, and recovered-thread relaunches skip it. Existing
  forge library repositories are left in place.

- Ready graph PRs automatically queue one maintainer child in their
  owning live run, sharing its session budget, allocation, and cancellation.
  A live maintenance queue and PR lifecycle notices keep newly ready work from
  being abandoned; the maintainer stop gate rechecks that queue. The public
  profile is now `horizon-maintainer` (`reviewer` remains a compatibility alias),
  with a dedicated graph maintenance skill. Managed PR creation and
  periodic discovery now enforce repository and wait-state labels, request the
  `horizon-maintainer` reviewer, reject merges while awaiting the author, and
  replace stale wait labels with a terminal label after merge or closure.

- Missions can pin nodes that exist only in graph pull requests through
  `metadata.node_attempts`. Open, closed, rejected, superseded, and merged PR
  versions remain addressable without becoming canonical DAG state; worker
  startup includes the proposed node document, and
  `horizon node show PROJECT NODE --pull N` reads it directly. New graph PRs
  receive a concise purpose and changed-node/impact body instead of being
  created without a description.

- Prompt runs now use explicit `horizon-collaborator` and
  `horizon-maintainer` Markdown profiles, independent of specialty templates and
  harness selection. Runs default to collaborator credentials; collaborators
  can queue scoped maintenance batches, and maintainers can return implementation
  work to collaborators without changing the caller's credential. Each session
  starts with a live briefing of global running/queued work, eligible slots,
  open Library/DAG PRs, and review wait states. Managed prompt reports use an
  exact five-section checklist and a durable stop gate; an incomplete mission
  or unused capacity continues scheduling, and a pending live audit cannot race
  a terminal report outcome.

- A node is one Markdown file (`node.md`) with Git-like history stored in
  SQLite. Object IDs are the SHA-1 Git would assign to a repository that
  contains only that file: blob of the UTF-8 bytes, a one-entry tree
  (`100644 node.md`), and commits whose `tree` is that tree. `HEAD` is
  `refs/heads/main`; open PRs are `refs/pull/{n}/head`. Collaborators propose
  node creation, node edits, and DAG wiring in atomic, multi-node graph pull
  requests. Maintainers can auditably amend a changeset before merge, and PR
  impact reports identify affected dependants, missing nodes, and cycles.
  Simultaneous progress `labels` replace the exclusive node `stage`; legacy
  stages migrate on write, and synthetic warning revisions are no longer
  created. The dashboard node page
  shows the accepted Markdown, child nodes, and PRs. Forgejo links are
  relative to the dashboard host and open the node's `node.md`. Selecting a
  PR renders the node as if that change were accepted.
  Published Lean lives in the shared workspace; a historical
  `library-{project}` Forge repository may still exist.

- The dashboard no longer polls compact state, missions, or Activity.
  Those views load on open or on **Refresh**. Compact `/api/v2/state`
  is view-scoped (`projects`, `hosts`, `activity`) so unused catalogs,
  Markdown bodies, and graph activity stay off the wire. Objective and
  mission lists omit document bodies until an item is opened. Node
  listings omit live-run attachments unless `include_activity=true`.
  Markdown skips KaTeX unless the page contains math, and DAG math
  labels render for the selected node only. Node directory search still
  pages from SQLite.

- Node directory search and pagination read a SQLite current-claim index
  (title/label/id/tags plus dependency edges) instead of materializing the
  compact graph projection. Statement bodies are still excluded from search.

- Control-plane Python modules live under semantic packages in
  `src/archon_horizon/platform/` (`graph`, `control`, `api`, `scheduler`,
  `hosts`, `integrations`, `workers`). Public names on
  `archon_horizon.platform` are unchanged. Import modules from the new
  packages (`archon_horizon.platform.api.http`, and so on). Reinstall
  managed Forge Lean-metrics hooks after upgrade so they invoke
  `python -m archon_horizon.platform.integrations.forge_metrics`.

- Remote Tailscale Serve access is no longer treated as the loopback
  maintainer console. Serve identity and forwarded headers require a
  Horizon account even when the TCP peer is `127.0.0.1`. The live
  dashboard proxy forwards Host and Tailscale headers to the API and
  does not answer `/api/v2/state` from its local read replica.

- Single-machine initialization is non-interactive. `horizon init` writes
  API, local user, and worker profiles without prompting; flags override
  bind, port, workers, forge, and Tailscale. Loopback dashboard access is
  the builtin maintainer console. Remote access uses Horizon accounts
  (hashed passwords, viewer/operator/maintainer roles, optional hashed API
  keys). New signups start as viewers. Managed Forge and Zulip identities
  are created per account and never shown as API keys. Each Forge user is
  added to project organizations on `horizon-viewers`, `horizon-operators`,
  or `horizon-maintainers` according to the Horizon role.

- Lean search splits Mathlib, published workspace, and extra libraries.
  Informal Mathlib uses the LSP `lean_leansearch` tool; type-shaped Mathlib uses
  `lean_loogle`. Published project Lean is indexed from the forge default branch
  (`GET /api/v2/projects/{project}/search`). Agents can add search-only clones
  (`POST /api/v2/search/libraries` with name, git URL, and commit) and query
  them without compiling. Maintainers can remove a clone with
  `DELETE /api/v2/search/libraries/{name}`. The dashboard Search tab exposes
  the same workspace and pool queries, plus library add/remove. Local
  `lean_search` / `horizon search` still cover the worker checkout, including
  module headers. Text and header ranking use bm25s (NumPy/SciPy) and persist
  the inverted index next to the declaration cache. Name search uses an
  exact/prefix index (no subsequence ranking). Type patterns bind `?a`/`_` to
  one identifier or bracket group. Cold index builds extract Lean files in
  parallel. Search API calls and LeanSearch / Loogle MCP queries appear on the
  session Activity timeline. Indexes are complementary to walking the actual
  trees: agents should still `ls`/`rg` workspace, Library, graph, Mathlib, and
  extra-library checkouts, and may inspect public repositories and pull
  requests. An empty query is not evidence that a listed source is empty.

- Zulip topic titles expose a leading status emoji (`💬` open, `⚠️` waiting,
  `📣` operator, `✅` resolved, `💾` memory, `🔧` infrastructure, `🚫` stuck).
  `GET /api/v2/zulip/topics` accepts `status` and `emoji` filters and returns
  `status`, `mark`, and `title` on each topic. `horizon zulip topics` lists
  them; `horizon zulip prefix-topics` adds the channel default to unmarked
  titles. Queue planners rank `mission_candidates` by priority then depth and
  inspect waiting/open/stuck/operator titles before dispatch.

- Graph node guidance requires semantic kebab-case labels, short mathematical
  titles, informal descriptions in textbook prose, and a `## References`
  section with a fenced BibTeX entry plus the source theorem number or
  section and page. Workers must not prefix `lem-`/`thm-`/`def-` or encode
  Lean types and implementation adjectives in the label, title, or informal
  body.

- Harness configuration refreshes register profiles without forcing a full
  scheduler pass for each host. The background scheduler admits queued work
  using the updated profiles while existing worker processes continue.

- Agent writing guidance favors concise Markdown bullets, descriptive links,
  node/proof references, and textbook-style mathematical statements. Zulip posts
  use linked attribution footers and readable session mentions, preserving
  legacy provenance and notification detection. Reports link recognized proof,
  session, and workspace commit IDs without rewriting their stored content.

- Lean source metrics retain library LOC and lexical axiom counts when a file
  has an unterminated comment or literal. Commit checks and reports show the
  exact source warning and location; malformed files cannot produce a clean
  axiom check or suppress metrics for the rest of MorganTianLib.

- Quota/balance and startup failures retry the same session behind shared
  cooldowns; unchanged queue planners back off. Worker startup errors retain
  redacted phase/traceback diagnostics. Full disks and transient database errors
  preserve completed worker results and command receipts without rerunning work.
  Cleanup protects preparing sessions, ignored sources and operator locks, and
  telemetry errors no longer terminate the harness. API retries respect
  Retry-After and preserve admission keys after malformed responses. Disconnected
  response clients close cleanly without repeated error writes and tracebacks.
  Background notification fetches back off during API or local inbox outages.
  Assignment prompts bound legacy mission excerpts and omit the full catalog
  index; worker guidance prioritizes Lean repair loops and concise linked evidence.

- Coordination notices are batched after 20 tools and two minutes, with up to
  three titles and prompt delivery for direct mentions. Worker Zulip posts
  notify mentioned sessions; mission events notify their parent/coordinator.
  Run frontiers suggest deep unfinished missions and expose node overlaps.
  Workers use short missions, optional history lookups and sustained Lean
  edit/check/fix work; default prompts omit ancestor mission documents.
  Infrastructure retries also refresh the assignment. Active DAG nodes retain
  their proof-status fill with a pink outline, and queue planners do not mark
  their root mission's nodes active.

- Node and DAG views show clickable tags for a node's running sessions and for
  the sessions that created or last updated the node or its proofs.

- Activity occupancy uses running sessions against the run's available
  scheduler slots, queued counts, and clickable running-session chips. The
  agent tree can show only live sessions, mission tags include involved nodes
  with the same hover previews as objectives, and a successful
  coordination-hook delivery is recorded as a notification event.

- The missions dashboard pages compact summaries instead of waiting for every
  mission body, and shows a loading state until the first page arrives.

- Release archives include the frontend rebuild inputs, tests, documentation,
  and notices, with CI checks excluding local configuration and operator state.
  Development instructions use the tracked skill catalog; `CLAUDE.md` imports
  `AGENTS.md`. Obsolete frontend screens and empty legacy directories have been
  removed, and pytest discovery is scoped to Horizon's own tests.

- Zulip uses one channel per project plus shared `guide`, `issues`, and
  `blockers` channels. `guide` is a short human reference for topic marks;
  agents may read it and cannot post there. Project channels are named from
  the project title. New projects receive a kebab-case id from that title.
  Managed setup removes Zulip's default `general`/`sandbox`/`zulip` streams.
  Worker posts get a server-added
  provenance header. Agents follow `horizon-zulip` for concise, human
  discussion rather than a progress log.

- Managed Lean checks prefer LSP feedback during editing and coordinate necessary
  builds across hosts using expiring leases and streamed SHA-256 artifact storage.
  Compatible Lake versions reuse actual root and dependency artifacts, with
  separate worker and maintainer caches. Each host shares Lake artifacts and
  dependency Git objects across checkouts; reflinks reduce source duplication
  where supported. The current worker and build setup is in the
  [pipeline setup guide](pipeline-setup.md#managed-lean-checks).

- The formalization graph holds statements, proof alternatives, source evidence,
  and failed attempts. Objectives hold roadmaps, missions hold executable
  assignments, and project workspaces preserve shared mathematical files.
  Packaged skills follow this control-plane workflow.

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
  guidance was recorded in the 0.1.5 development history.
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
the 0.1.2 development history.

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

[Unreleased]: https://github.com/frenzymath/Archon-Horizon/compare/v0.2.0-alpha.2...HEAD
[0.2.0-alpha.2]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.5...v0.2.0-alpha.2
[0.1.5]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/frenzymath/Archon-Horizon/compare/v0.1.2...v0.1.3
[0.1.0]: https://github.com/frenzymath/Archon-Horizon/releases/tag/v0.1.0
