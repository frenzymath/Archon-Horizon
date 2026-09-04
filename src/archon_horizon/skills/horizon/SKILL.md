---
name: horizon
description: How to work inside an Archon Horizon workspace — orient from on-disk state, pick the highest-value formalization work, record progress as commits, and coordinate. Load this first; it points to the specialized skills for detail. Editable per workspace.
---

You are working inside an **Archon Horizon** workspace: a place where AI agents
formalize mathematics in Lean 4 across one or more projects. This skill is the
map. It is deliberately short and **you can edit it** (it lives at
`.claude/skills/horizon/SKILL.md` in this workspace) — tune it to how you want to
work. It points to focused skills; load those on demand rather than up front.

## You are a team; the workspace is where teams collaborate

Think of your session as **one team**: you (the lead) plus the subagents you
spawn as your **workers**, with the tools and skills available to you. A
`horizon run` is your team working through its task. Other runs are **other
teams** working in parallel on the *same shared workspace* — one Lean project set,
one roadmap/board, one ledger, one inbox.

You do not manage other teams and they do not manage you. You coordinate through
**shared state**, not meetings:

- **The board (roadmap)** is the shared plan. Keep your items' status/owner/
  milestone current so other teams see what you hold and where it's going.
- **The inbox** is asynchronous messaging + memory across teams (skill:
  `horizon-inbox`): shared notes, per-task private items, and direct messages to
  another team.
- **Commits** are the durable record; reading recent ledger history tells you what
  other teams just produced (skill: `project-git`).

Before you start, be aware of who else is live (see "Is another run live" below).
A short **synchronizer** digest is printed to stderr at the start of your CLI
commands. Its attention order is deliberate: `REQUIRED` active protections,
`ACTION` unread conversations, advisory unread inbox, then session/runtime and
other live runs. Read it; it is how you stay aware without asking anyone. A new
conversation or reply invalidates the digest cache immediately. If stderr is
redirected, the same protection/conversation lanes are the first `attention`
object in `horizon inbox list --json`.

## Orient (pull state, don't assume it)

**Start here:** your prompt says nothing about who ran before you. Usually
somebody did — often minutes ago, often on this same task, and their report was
written for you. Skill: `horizon-start` — one cheap pass that works out which
situation you were launched into (fresh run, hand-off from the session that just
finished, resume after a crash, or outside a run) and what to read for each.

The workspace root is `$ARCHON_HORIZON_ROOT`; your shell may start in a member
project rather than at that root. The absolute path to this skill is
`$ARCHON_HORIZON_SKILL`. Live state is under
`$ARCHON_HORIZON_ROOT/.archon-horizon/` and the manifest is
`$ARCHON_HORIZON_ROOT/config.yaml`. Read what you need, when you need it — via the
`horizon` CLI (invoke it as `"$HORIZON_BIN" …`):

- **Roadmap** — YOUR strategy sketch across *all* projects, kept as a nested
  outline. **You own it.** An empty roadmap is unfinished orientation, not a
  green light to improvise only in Lean: draft the plan before (or as) you start
  the first substantive proof. `"$HORIZON_BIN" roadmap list` renders the indented
  tree with per-parent progress (`active · 3/7 done`); `--focus <id>` shows one
  subtree, `--max-depth 0` the top level only. Structure it as you reason —
  do not wait for a human to seed it, and do not only flip status on items that
  already exist:
  - **Create** missing goals/sub-goals: `roadmap add --id … --title … [--parent …]
    [--depends-on …] [--milestone …] [--owner …]`.
  - **Move / re-nest** when the route changes: `roadmap set <id> --parent <p>`
    (or `--parent ''` to un-nest), `--depth N`, `--depends-on …`.
  - **Retire stale plan**: `roadmap set <id> --status rejected` or
    `roadmap remove <id>` (children un-nest by default; `--cascade` deletes them).
  - **Rename** when an id no longer matches the math: `roadmap rename <old> <new>`
    (rewrites parent/depends links).
  - **Track deliverables**: `--pin-commit <sha>`, status/summary updates, and
    link the running task with `"$HORIZON_BIN" task set "$ARCHON_HORIZON_TASK"
    --roadmap-ref <id>`.
  It doubles as the **project board** (`--owner`, `--milestone`, pin commits).
  Commands warn when parent/child statuses disagree or too many milestones are
  simultaneously `active` — nothing is auto-corrected; you decide.
- **Tasks** — a specific piece of work, usually the one a human launched and is
  watching. `"$HORIZON_BIN" task …`. Task commands warn when the open queue grows
  beyond the advisory limit or a `running` status looks orphaned.
- **Inbox** — how teams talk across sessions and projects. Leave a note for the
  next session; read what past ones left. `"$HORIZON_BIN" inbox …` (skill:
  `horizon-inbox`). Items can be **owned by your task** (private, e.g. your team's
  memory) or **shared with everyone** (the default); read-state is per-team, so
  `inbox list --mine --unread` is your team's fresh queue, and you can
  direct-message another team with `--to task:<id>`. Inbox commands warn when the
  open working set—especially `memory` and `info`—needs review.
- **Inbox attention order** — before edits, inspect all open `protection` items
  (standing constraints, whether read or unread), then open unread
  `conversation` items and reply or mark them read. Other kinds are advisory
  context. In an agent session an unqualified `inbox list` defaults to open items
  and sorts in this order; use an explicit status to audit history.
- **Conversation discipline** — use a conversation only when a reply/decision is
  expected. Search and reuse an open thread before starting one. Its initiator is
  a participant and owns closure: after consuming the answer, add a concise
  conclusion if needed and archive the thread. Reply to human-started threads but
  leave their closure to the human unless asked otherwise (skill: `horizon-inbox`).
- **Blueprint graph** — declaration dependencies and what's proved. `"$HORIZON_BIN" graph -p <project> …` (skill: `hgraph`).
  Each node is also an **hgraph** file with attached comments/reviews —
  `"$HORIZON_BIN" graph -p <project> frontier` ranks what to prove next, and node-scoped failure memory
  goes on the node itself (skill: `hgraph`).
- **Memory** — durable facts/dead-ends live in the inbox: read with
  `"$HORIZON_BIN" inbox list --kind memory --json`, write with
  `"$HORIZON_BIN" inbox add --kind memory --to horizon --body …`.
- **Usage** — your token/cost consumption this session and run, budget headroom,
  and recent rate-limit signals: `"$HORIZON_BIN" usage --json`. See
  "Pace yourself" below for how to act on it.

## Your session's identity (environment variables)

The harness exports these to every session — read them instead of guessing:

| Variable | Meaning |
|---|---|
| `ARCHON_HORIZON_ROOT` | workspace root (use for `--root`-free CLI calls) |
| `ARCHON_HORIZON_SKILL` | absolute path to this `SKILL.md` (never resolve it relative to the shell cwd) |
| `ARCHON_HORIZON_RUN` | run id (e.g. `0163`) |
| `ARCHON_HORIZON_SESSION` | this session's name (e.g. `0002-horizon-T-1`) |
| `ARCHON_HORIZON_SESSION_DIR` | this session's directory (transcript, usage.json, report) |
| `ARCHON_HORIZON_ROUND` / `ARCHON_HORIZON_ROUNDS` | which round this is (0-based) / the run's planned total |
| `ARCHON_HORIZON_TASK` / `ARCHON_HORIZON_TASK_TITLE` | the task id / title (full body: `"$HORIZON_BIN" task show "$ARCHON_HORIZON_TASK" --json`) |
| `ARCHON_HORIZON_PROJECTS` | comma-separated projects this task spans |
| `ARCHON_HORIZON_TMP` | disposable per-session scratch directory; temp-aware tools are routed here instead of `/tmp` |
| `ARCHON_HORIZON_TMP_ROOT` | workspace scratch root (`.archon-horizon/tmp/`) for safe stale cleanup |
| `HORIZON_BIN`, `HORIZON_GIT` | absolute paths to the CLI and the ledger-git wrapper |
| `HORIZON_LEDGER_GIT_DIR`, `HORIZON_LEDGER_WORK_TREE` | the workspace ledger repo + its work tree — `"$HORIZON_GIT" …` is shorthand for `git --git-dir "$HORIZON_LEDGER_GIT_DIR" --work-tree "$HORIZON_LEDGER_WORK_TREE" …` |

## Disposable scratch (quota-safe)

Use `$ARCHON_HORIZON_TMP` for downloads, generated source, extraction trees,
large probes, and other files that do not belong in the project or the durable
session artifact directory. Horizon sets `TMPDIR`, `TMP`, and `TEMP` to this
per-session path before launching the engine, so Python, Lean tooling, and shell
children that honor standard temporary-directory variables follow the same
route. If an older parent session has not exported the variables yet, initialize
the fallback explicitly before a large probe:

```bash
export ARCHON_HORIZON_TMP="${ARCHON_HORIZON_TMP:-$ARCHON_HORIZON_ROOT/.archon-horizon/tmp/${ARCHON_HORIZON_RUN:-adhoc}/${ARCHON_HORIZON_SESSION:-manual}}"
mkdir -p "$ARCHON_HORIZON_TMP"
export TMPDIR="$ARCHON_HORIZON_TMP" TMP="$ARCHON_HORIZON_TMP" TEMP="$ARCHON_HORIZON_TMP"
```

Do not put reports, evidence, rejected attempts, or source changes here:
copy durable material into the session/report or the ledger before the session
ends.

The path is normally removed automatically after the invocation. If a process
crashes or a manually-created scratch tree remains, inspect it with
`"$HORIZON_BIN" tmp clean --older-than-hours 24 --json` and apply cleanup only
with `--apply`; the command protects scratch belonging to live run markers.
Never delete another run's active scratch directory by hand. At a janitor
checkpoint (roughly every second session and before a final report), perform
that dry-run check and apply it when the candidates are clearly stale.

## Pace yourself (usage & interruption risk)

Sessions end for reasons you don't control: rate limits, usage caps, budgets.
The defense is NOT to avoid work — it is to make interruption cheap:

- **Commit at every coherent point** (plain git into the ledger, below). If
  `"$HORIZON_BIN" usage --json` shows low budget headroom or recent
  `rate_limit`/`usage_limit` signals, commit what you have NOW and prefer
  finishing the piece in flight over opening a new front.
- If this is the run's **last round** (`ARCHON_HORIZON_ROUND` + 1 ==
  `ARCHON_HORIZON_ROUNDS`), leave the workspace hand-off-clean: commit, update
  the roadmap/task status, and write the report as if nobody continues today.
- Interrupted anyway? Nothing is lost that was committed — the next session
  resumes from the ledger (see "Resuming" below).

## One-shot discipline (important)

This session runs to completion and is **not resumed** — you will not be re-invoked
when a background job finishes. So **run work in the foreground and block on it**
(builds, checks, subagents). Anything you leave running in the background, or leave
uncommitted, may be lost when the session ends. Commit early and often (below).

## Pick the highest-value work

Read the roadmap and the live Lean state, then commit to the most valuable next
piece. **If the roadmap is empty or silent on your task's objective, build it
first** (or in the same breath as the first commit): a coarse nested outline of
the source-facing frontier, the next few producers, and what is blocked/out of
scope. Strategy lives on the roadmap so the next session does not re-derive it
from transcripts. Reshape the tree whenever the route pivots — add a missing
prerequisite, move a sub-goal under a new parent, reject a dead branch, pin the
commit that closed a node. Status-only updates on a frozen outline are not
enough when the plan itself changed.

A node being large, multi-session, or blocked on missing mathlib infrastructure
is **not** a reason to skip it — start it, build the missing lemma/definition
yourself as project-local infrastructure, and push it as far as you genuinely
can. Prefer ambitious progress over defensive avoidance.

If this session was launched on a specific task, make concrete progress on that
task's REAL objective — don't preemptively pivot to easy unrelated wins because
the objective is large. Leave a concise task comment only for a durable state
change or blocker, not every action. You own the task's terminal status (skill:
`task-status`): set `--status done` only when the objective is FULLY complete;
`blocked`/`failed` if genuinely stuck; set nothing if it's only partly advanced
(it returns to the queue).

## Fresh-context checkpoints

The old Ground pass is no longer a second orchestrator role, but independent
review remains a useful convergence option. The following checkpoints are
advisory signals: choose the helper and scope that fit the work, and do not treat
them as a runtime gate. Consider spawning the **`ground`** subagent at these
checkpoints:

- before marking a multi-step task `done`;
- after every two substantive Horizon sessions on a long-running task;
- immediately after a strategy pivot, a broad workspace edit, or a surprising
  clean/build result.

Give it the task/project scope and ask it to inspect the actual ledger diff,
blueprint graph, Lean state, roadmap, inbox, and reports with fresh context. It
is read-only on source and reports issues/memory; reconcile its findings before
continuing. Use **`work-reviewer`** for a progress-integrity audit of one task's
objective, report, diff, and history; route mathematical/source, blueprint,
verification, API, and architecture questions to their corresponding
specialist reviewers. Use **`janitor`** when the main concern is workspace
hygiene. A one-session task may skip the
periodic checkpoint, and a fresh-context review before a terminal `done` claim
is a useful confidence check when the scope warrants it.

Consider upkeep even when the last command did not print a warning. On a
multi-session run, the optional recommendation to dispatch **`janitor` at the
start of every second Horizon session** and before the final report applies; on a
one-session task, consider dispatching it once before claiming completion if the
run touched roadmap, task, or inbox state. Record the checkpoint in the report
and wait for a helper you chose before continuing.
If `janitor` is unavailable, ask **`ground`** for the same hygiene inspection;
Ground is read-only, so apply or explicitly record its findings yourself.

## Warnings are work

Commands report problems for a reason — never scroll past them. `lake build`
warnings, `horizon graph sync` warnings, roadmap consistency warnings, deprecation
notices from any tool: if your change caused it, **fix it now** (it is part of
the task); if it's pre-existing or caused by the tool/command itself, don't
silently ignore it — record it as a memory item, file an inbox `issue`, or
address it `--to human` when a human decision is needed. A warning that
survives your session should be one you *chose* to leave, with a trace saying
why.

Collection-health warnings are a dispatch trigger for operational hygiene, not
background noise. When `inbox`, `roadmap`, or `task` reports an overloaded queue,
stale running item, or status mismatch, pause the proof loop and dispatch
**`janitor`** with the workspace scope (or use the `ground` fallback). Wait for
the chosen helper,
reconcile its report, rerun the command, and record any warning that remains
intentionally. Do this at most once for the same warning in a session; a
persistent warning still needs a report or inbox issue rather than repeated
no-op calls. This targeted health response is separate from optional semantic
review checkpoints.

## Do the work (Lean)

For any Lean edit, load `lean-check` before touching the file and follow its
required LSP loop: query the target with `lean_diagnostic_messages` or
`lean_goal` before the first edit and after each subsequent edit. Keep `lake
build` for the final session boundary or a specifically required kernel check;
LSP is enough between edits and proof obligations. Use the narrowest `lake env
lean` fallback when LSP is unavailable, and do not duplicate that check when the
configured final build covers the same files. Run broad or final checks through
`"$HORIZON_BIN" check [lake-target ...]`; use `--lean FILE` for a serialized
file check when LSP is unavailable. Horizon gives these checks one shared
resource slot, coalesces identical concurrent requests, applies a timeout, and
records their result in the session.

**Do not `grep` for a lemma.** Grep matches names you already guessed; it cannot
find the lemma whose name you don't know, and that is the one that costs you an
afternoon. This workspace indexes every declaration in every project *and* in
mathlib — query it:

| You want | Use | Not |
|---|---|---|
| "does this lemma exist, anywhere?" | `"$HORIZON_BIN" search "<words or name>" --json` | `grep -r` |
| "what's the lemma for this *statement*?" | LSP `lean_leansearch` (natural language) | guessing names |
| "what matches this *type*?" | LSP `lean_loogle` (`Nat → ?a → ?a`) | `grep` |
| "does something in scope close this goal?" | LSP `lean_local_search` / `lean_hover_info` | reading files |
| "what should I prove next?" | `"$HORIZON_BIN" graph -p <project> frontier` (ranked) | scanning the blueprint |
| "what does this node depend on / block?" | `"$HORIZON_BIN" graph -p <project> get label:<id>` | reading imports |
| "is the proof right?" | `"$HORIZON_BIN" check --lean <file>` (narrowest faithful check) | `lean_diagnostic_messages` alone |

`"$HORIZON_BIN" search` covers **your projects and mathlib together**, which is
its whole point: the premise you need is usually already in mathlib under a name
you would never have grepped for. One query costs a second; re-proving an
existing lemma costs a session. If it reports a library as unindexed, fix that
first (`lake build`, then `"$HORIZON_BIN" search --reindex`) rather than falling
back to grep.

Grep is still the right tool for what it *is* good at: finding a known string, a
specific file, or every call site of a name you already have.

- Search before proving — the lemma may already exist. Skill: `leansearch`.
- Use the **Lean LSP MCP** for tight proof feedback, then verify with the narrowest
  faithful `lake` / `lake env lean` check. Skill: `lean-check`.
- Use the DAG to choose and scope work, not just to report it. Skill: `hgraph`.
- When editing blueprint material, follow the house format. Skill: `blueprint-conventions`.
  The blueprint is timeless mathematics, never a formalization journal, and it is
  the **first source of truth** before Lean: complete proofs, the chosen route
  only, bibliography + `\dcref`/`\source` with how the text adapts or differs
  from references. Refactor the blueprint when strategy changes; Lean follows.
  Put Lean implementation notes, failed tactics, and declaration-specific
  progress on the corresponding hgraph node with `graph add comment`; do not
  insert "Formalization note" paragraphs into blueprint `.tex` files.
- Write Lean in mathlib style (naming, module docs, tree layout). Skill:
  `mathlib-conventions`. Rank costly files with
  `"$HORIZON_BIN" benchmark [-p <project>] --json` (sum of `set_option`
  heartbeat budgets). Prefer a clean **module restart** over layered patches
  when the same blocker loops — skill: `restart-module`.
- **Definitions are often the root cause.** When theorems/lemmas around an API
  look suboptimal for mathematical reasons that should be easy, load
  `definition-quality` and consider dispatching **`definition-quality-reviewer`**
  with witness consumers: pain in consumers often means a bad underlying def
  (needless `abbrev` layers, lost defeq, deep instances), not only a hard proof.

## Source and semantic review (choose as needed)

For a source-backed definition, theorem, or exported API, consider loading the
`formalization-review` skill. It offers reusable questions about source shape,
proxy versus intrinsic objects, bridge lemmas, hypotheses and edge cases,
blueprint attachments, claim evidence, and API composition. Use `blueprint` to
author an assigned slice; ask `source-fidelity-reviewer` or
`blueprint-integrity-reviewer` for an independent scoped audit. Ask
`work-reviewer` only for a fresh progress-integrity audit of the task diff and
history. For architecture or dependency-route questions, use
`strategy-reviewer` for the project/roadmap route and `api-composition-reviewer`
for the public abstraction boundary. When proofs fight an encoding or several
lemmas share the same packaging pain, use `definition-quality-reviewer` on the
suspect defs and their consumers. For a new certificate/context package,
target-shaped assumption, `\leanok` or completion claim, or a task whose first unmet producer
has survived two rounds, run `honesty-reviewer` explicitly. If it is unavailable,
record the concrete skip reason in the final epistemic checkpoint.
It classifies proved versus conditional/imported versus empty or sorry-backed
certificates and compares the source-facing frontier for loops. If the task has
repeated wrappers or re-expressions, pair it with `strategy-reviewer` and record
the decision rather than silently dispatching another local helper. This
recommendation does not auto-dispatch a helper and does not replace your
judgement about scope.

As a rough risk signal: a tiny local edit may need no helper; a source-backed
definition, public API, or changed `\leanok` claim is a good reason to consider
the corresponding source, semantic, or blueprint-integrity reviewer; a task
with repeated rounds or a completion claim is a good reason to consider
`work-reviewer` and `honesty-reviewer`; a proof-heavy export or refactor may benefit from
`lean-quality-reviewer`; a proof-heavy export that may rest on a bad core def may
benefit from `definition-quality-reviewer`; a broad strategy/build or workspace
claim may benefit from `ground`; and a primarily imported/conditional boundary,
source transcription, release claim, or disputed high-severity finding may
benefit from the corresponding specialist. A primarily mechanical inbox or
documentation issue may fit `janitor`. Combine roles only when the extra context
is worth the cost.

## Read the other projects

They are in this workspace for a reason: the same lemma, pattern, or dead end has
often already been worked out next door. `"$HORIZON_BIN" search` spans **all**
projects — an existing construction in another project is a lead, whether you
import it, copy the approach, or read its blueprint. `references/` holds the
original sources (skill: `references`). Staying inside your own project because
the task named it is how the workspace re-derives the same thing three times.

## Delegating beyond your team (ask permission first)

Your normal way to parallelize is **within** your team: spawn subagents/workers
(skill: `subagents`) and dispatch scoped work to them in the foreground. That
needs no permission.

Launching work **outside** your team — creating new tasks for other teams, or
spawning a whole new `horizon run` — is different: it spends the user's compute
and accounts, so it is **off by default**. Before you even consider it, read the
standing consent: `"$HORIZON_BIN" permissions --json`. It reports
`allow_launch_tasks`, `allow_launch_runs`, `max_parallel_sessions`, any declared
`accounts`, and free-form notes the user left (e.g. which account to prefer, when
limits reset). If both `allow_*` are false (the default), **do not** create tasks
or launch runs on the user's behalf — instead leave an inbox item `--to human`
proposing the delegation and why. Only when a flag is enabled may you act within
its stated caps, and follow the account/usage notes the user recorded.

## Record progress = commit (this is how progress is read)

Your commits — message + diff — are the durable record of what you did; the
dashboard reads progress from them. Commit coherent progress yourself with plain
`git` into the workspace ledger. Skill: `project-git` (use `"$HORIZON_GIT" commit -m …`
or the explicit `--git-dir/--work-tree` form; provenance trailers are auto-stamped).

Write **semantic, math-first** messages that say what you proved/built. Prefer
staging explicit files over `add -A`. Beyond commits:

- Commit after each independently useful verified proof, file, or bounded tooling
  change; commit before long builds/reviews and before expanding scope. Multi-hour
  implementation sessions normally produce several commits.
- A rejected proof attempt is evidence, not a commit. Before deleting or replacing
  a substantial draft, preserve it with
  `"$HORIZON_BIN" attempt save <files...> --reason "why it failed"`
  (optionally `--diagnostics <file>`). The dashboard reports the artifact
  separately from durable commits, so the next session can inspect the dead end
  without restoring it to the working tree.
- Never end a session with durable authored changes after the last commit. The
  lifecycle hook gives a compact checkpoint reminder and may pause Stop when it
  observed a file mutation after the last commit.
- Keep operational prose as a delta. Task/roadmap/inbox comments default to one
  sentence or at most three bullets: conclusion, evidence, next action. Do not
  copy the report, commit summary, or thread history into them.

- Update the **roadmap** as a living plan: status, summary, nesting, depends-on,
  add/remove/rename items when the strategy changed — not only when a pre-existing
  row needs a status flip, and never a re-narration of the diff.
- Use the **inbox** to hand off to the next/other sessions; record durable dead-ends
  as **memory**.

Before your final report, do one boundary-maintenance pass. Re-read the task's
`roadmap_refs` and `inbox_refs` (create and link roadmap items if the task still
has none and the work is multi-step). Update each milestone whose status or
structure changed, add a concise mathematical comment for a key advance, and
archive or complete open inbox items your work actually resolved. Also scan the
remaining open inbox for consumed temporary/info/memory items and archive those
that are now stale. Never archive a standing protection merely to make the list
shorter. In particular, review every open conversation your task started: archive
answered threads and leave a concrete blocker on any that must stay open. This
pass is part of completing the work, not optional janitor follow-up.

## Resuming after an interruption

State lives on disk, so a fresh session (even on another account) continues
cheaply. Working out *whether* you're resuming, and what to read if you are, is
the `horizon-start` skill — load it at the start of the session. The essentials:

- **Why did the last run stop?** `"$HORIZON_BIN" usage --json` includes a
  `paused` field (from `runs/<id>/paused.json`) with the reason (usage limit,
  budget, …) and the exact resume command; `recent_failure_reasons` shows
  rate-limit signals. A paused/killed run resumes with
  `"$HORIZON_BIN" run --resume <id>`.
- **What was in flight?** Read recent ledger history (`project-git` skill:
  `git log` + `git show` by `Archon-Session` trailer) and the previous
  session's transcript/report under `.archon-horizon/runs/`. A horizon session
  directory with **no `report.md`** is one that was killed mid-flight.
- **Is another run live on this workspace?** `"$HORIZON_BIN" ps` lists runs
  holding a process (the ledger is one shared branch — be aware of parallel
  writers); it also flags zombie markers and stalled runs. A run registers a
  `runs/<id>/process.json` marker (pid/host) at start and removes it on clean
  exit; `ps` decides liveness by probing the pid on the local host (a marker whose
  pid is dead is a reap-able zombie, a live pid idle for a long time is "stalled").
  The synchronizer surfaces the same "other runs live" signal at command start, so
  you usually don't need to call `ps` explicitly.

## Final report (your last message)

Your final message is saved as the session's report — a human or the next
session should understand the session from it without opening raw logs.
Use only sections that carry information: normally `## Progress`, `## Issues`,
`## Why I stopped`, and `## Next`. In `## Progress`, use one inline `-` bullet per file/target,
e.g. `- FileA.lean: 4 sorries -> 3; closed the base case.` In `## Why I
stopped`, say plainly whether the objective is fully complete, partly advanced,
or blocked — and why. Always mention build failures, broken proofs, blocked
dependencies, and checks that failed or were not run. If a plausible next
action fits in the session's scope, take it before stopping — a clean commit is
not by itself a reason to stop. Do not replay the chronological session log.

For source-backed formalization, add a compact epistemic checkpoint to the same
report (it is not needed for an ordinary tooling task):
`Claim class: proved producer | conditional interface | imported boundary |
empty/vacuous | axiom/sorry-backed | unverified`; `Frontier before/after:` the
source-facing node and first unmet producer; `Consumer:` the intended downstream
declaration/node; and `Evidence:` the exact target/build, transitive `#print axioms`,
and source/graph status. State `honesty-reviewer: used` or `skipped — <reason>`
when a certificate, completion claim, or repeated frontier triggered the lane.
A conditional or imported class may be a useful handoff, but it must not be
described as discharging the source theorem or justify a source-facing `\leanok`.

## Cleaning up the work (you decide, via subagents)

There is no second orchestrator role running alongside you. *You* are the driver,
and the **`ground`** subagent is an available fresh-context checkpoint when the
workspace or strategy benefits from an external view. Available helpers (see the
`subagents` skill):

- **janitor** — workspace hygiene: roadmap/READMEs concise, inbox from overflowing,
  **writable** Lean/blueprint layout moves and renames (not proof redesign).
- **ground** — workspace-wide strategy, graph, ledger, and convergence review.
- **work-reviewer** — progress-integrity review of one task; is the claimed work
  real, task-scoped, and converging rather than looping?
- **honesty-reviewer** — anti-evasion review of certificates, completion claims,
  hidden assumptions, vacuity, and repeated unchanged frontiers.
- **blueprint** — Lean ↔ blueprint statement/`\uses` correctness for a scoped slice.
- **reference-retriever**, **debug**, **page-transcriber** — as needed.

Keep proving; delegate the upkeep.
