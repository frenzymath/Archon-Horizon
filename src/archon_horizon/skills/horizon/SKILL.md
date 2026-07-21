---
name: horizon
description: How to work inside an Archon Horizon workspace — orient from on-disk state, pick the highest-value formalization work, record progress as commits, and coordinate. Load this first; it points to the specialized skills for detail. Editable per workspace.
---

You are working inside an **Archon Horizon** workspace: a place where AI agents
formalize mathematics in Lean 4 across one or more projects. This skill is the
map. It is deliberately short and **you can edit it** (it lives at
`.claude/skills/horizon/SKILL.md` in this workspace) — tune it to how you want to
work. It points to focused skills; load those on demand rather than up front.

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
  outline. `"$HORIZON_BIN" roadmap list` renders the indented tree with per-parent
  progress (`active · 3/7 done`); `--focus <id>` shows one subtree, `--max-depth 0`
  the top level only. Structure it: nest sub-goals with `--parent`, keep your
  item's status/strategy current as you work. Roadmap commands print a **warning**
  when parent/child statuses disagree (all sub-items done but parent open, or a
  done parent with open children) — nothing is auto-corrected; you decide whether
  to fix it or leave it (it may be intentional). They also warn when too many
  milestones claim simultaneous `active` focus.
- **Tasks** — a specific piece of work, usually the one a human launched and is
  watching. `"$HORIZON_BIN" task …`. Task commands warn when the open queue grows
  beyond the advisory limit or a `running` status looks orphaned.
- **Inbox** — how agents talk across sessions (and projects). Leave a note for the
  next session; read what past ones left. `"$HORIZON_BIN" inbox …` (skill:
  `horizon-inbox`). Inbox commands warn when the open working set—especially
  `memory` and `info`—needs review.
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
| `HORIZON_BIN`, `HORIZON_GIT` | absolute paths to the CLI and the ledger-git wrapper |
| `HORIZON_LEDGER_GIT_DIR`, `HORIZON_LEDGER_WORK_TREE` | the workspace ledger repo + its work tree — `"$HORIZON_GIT" …` is shorthand for `git --git-dir "$HORIZON_LEDGER_GIT_DIR" --work-tree "$HORIZON_LEDGER_WORK_TREE" …` |

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
piece. A node being large, multi-session, or blocked on missing mathlib
infrastructure is **not** a reason to skip it — start it, build the missing
lemma/definition yourself as project-local infrastructure, and push it as far as
you genuinely can. Prefer ambitious progress over defensive avoidance.

If this session was launched on a specific task, make concrete progress on that
task's REAL objective — don't preemptively pivot to easy unrelated wins because
the objective is large. Leave a `"$HORIZON_BIN" task comment <task_id> --body …`
at each significant step. You own the task's terminal status (skill:
`task-status`): set `--status done` only when the objective is FULLY complete;
`blocked`/`failed` if genuinely stuck; set nothing if it's only partly advanced
(it returns to the queue).

## Fresh-context checkpoints

The old Ground pass is no longer a second orchestrator role, but independent
review is still part of convergence. Spawn the **`ground`** subagent at these
checkpoints:

- before marking a multi-step task `done`;
- after every two substantive Horizon sessions on a long-running task;
- immediately after a strategy pivot, a broad workspace edit, or a surprising
  clean/build result.

Give it the task/project scope and ask it to inspect the actual ledger diff,
blueprint graph, Lean state, roadmap, inbox, and reports with fresh context. It
is read-only on source and reports issues/memory; reconcile its findings before
continuing. Use **`work-reviewer`** for a narrow diff/proof audit and **`janitor`**
when the main concern is workspace hygiene. A one-session task may skip the
periodic checkpoint, but must still obtain a fresh-context review before a
terminal `done` claim.

## Warnings are work

Commands report problems for a reason — never scroll past them. `lake build`
warnings, `horizon graph sync` warnings, roadmap consistency warnings, deprecation
notices from any tool: if your change caused it, **fix it now** (it is part of
the task); if it's pre-existing or caused by the tool/command itself, don't
silently ignore it — record it as a memory item, file an inbox `issue`, or
address it `--to human` when a human decision is needed. A warning that
survives your session should be one you *chose* to leave, with a trace saying
why.

## Do the work (Lean)

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
| "is the proof right?" | `lake env lean <file>` (narrowest faithful check) | `lean_diagnostic_messages` alone |

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
  The blueprint is timeless mathematics, never a formalization journal. Put Lean
  implementation notes, failed tactics, and declaration-specific progress on the
  corresponding hgraph node with `graph add comment`; do not insert
  "Formalization note" paragraphs into blueprint `.tex` files.

## Read the other projects

They are in this workspace for a reason: the same lemma, pattern, or dead end has
often already been worked out next door. `"$HORIZON_BIN" search` spans **all**
projects — an existing construction in another project is a lead, whether you
import it, copy the approach, or read its blueprint. `references/` holds the
original sources (skill: `references`). Staying inside your own project because
the task named it is how the workspace re-derives the same thing three times.

## Record progress = commit (this is how progress is read)

Your commits — message + diff — are the durable record of what you did; the
dashboard reads progress from them. Commit coherent progress yourself with plain
`git` into the workspace ledger. Skill: `project-git` (use `"$HORIZON_GIT" commit -m …`
or the explicit `--git-dir/--work-tree` form; provenance trailers are auto-stamped).

Write **semantic, math-first** messages that say what you proved/built. Prefer
staging explicit files over `add -A`. Beyond commits:

- Update the **roadmap** with coarse status/strategy (not a re-narration of the diff).
- Use the **inbox** to hand off to the next/other sessions; record durable dead-ends
  as **memory**.

Before your final report, do one boundary-maintenance pass. Re-read the task's
`roadmap_refs` and `inbox_refs`; update each roadmap milestone whose status or
strategy changed, add a concise mathematical comment for a key advance, and
archive or complete open inbox items your work actually resolved. Also scan the
remaining open inbox for consumed temporary/info/memory items and archive those
that are now stale. Never archive a standing protection merely to make the list
shorter. This pass is part of completing the work, not optional janitor follow-up.

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
  writers); it also flags zombie markers and stalled runs.

## Final report (your last message)

Your final message is saved as the session's report — a human or the next
session should understand the session from it without opening raw logs.
Recommended sections: `## Summary`, `## Progress`, `## Issues`,
`## Why I stopped`, `## Next` (keep at least `## Progress` and
`## Why I stopped`). In `## Progress`, one inline `-` bullet per file/target,
e.g. `- FileA.lean: 4 sorries -> 3; closed the base case.` In `## Why I
stopped`, say plainly whether the objective is fully complete, partly advanced,
or blocked — and why. Always mention build failures, broken proofs, blocked
dependencies, and checks that failed or were not run. If a plausible next
action fits in the session's scope, take it before stopping — a clean commit is
not by itself a reason to stop.

## Cleaning up the work (you decide, via subagents)

There is no second orchestrator role running alongside you. *You* are the driver,
and the **`ground`** subagent is the scheduled fresh-context checkpoint when the
workspace or strategy needs an external view. Available helpers (see the
`subagents` skill):

- **janitor** — workspace hygiene: roadmap/READMEs concise, inbox from overflowing.
- **ground** — workspace-wide strategy, graph, ledger, and convergence review.
- **work-reviewer** — fresh-context review of your last work; is it converging?
- **blueprint** — Lean ↔ blueprint statement/`\uses` correctness for a scoped slice.
- **reference-retriever**, **debug**, **page-transcriber** — as needed.

Keep proving; delegate the upkeep.
