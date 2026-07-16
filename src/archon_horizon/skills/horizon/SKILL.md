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

The workspace root is `$ARCHON_HORIZON_ROOT`. Live state is under `.archon-horizon/`
and the manifest is `config.yaml`. Read what you need, when you need it — via the
`horizon` CLI (invoke it as `"$HORIZON_BIN" …`):

- **Roadmap** — YOUR strategy sketch across *all* projects, kept as a nested
  outline. `"$HORIZON_BIN" roadmap list` renders the indented tree with per-parent
  progress (`active · 3/7 done`); `--focus <id>` shows one subtree, `--max-depth 0`
  the top level only. Structure it: nest sub-goals with `--parent`, keep your
  item's status/strategy current as you work. Roadmap commands print a **warning**
  when parent/child statuses disagree (all sub-items done but parent open, or a
  done parent with open children) — nothing is auto-corrected; you decide whether
  to fix it or leave it (it may be intentional).
- **Tasks** — a specific piece of work, usually the one a human launched and is
  watching. `"$HORIZON_BIN" task …`
- **Inbox** — how agents talk across sessions (and projects). Leave a note for the
  next session; read what past ones left. `"$HORIZON_BIN" inbox …` (skill: `horizon-inbox`)
- **Blueprint DAG** — declaration dependencies and what's proved. `"$HORIZON_BIN" leandag …` (skill: `leandag`).
  Each node is also an **hgraph** file with attached comments/reviews —
  `hgraph frontier` ranks what to prove next, and node-scoped failure memory
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

## Warnings are work

Commands report problems for a reason — never scroll past them. `lake build`
warnings, `hgraph sync` warnings, roadmap consistency warnings, deprecation
notices from any tool: if your change caused it, **fix it now** (it is part of
the task); if it's pre-existing or caused by the tool/command itself, don't
silently ignore it — record it as a memory item, file an inbox `issue`, or
address it `--to human` when a human decision is needed. A warning that
survives your session should be one you *chose* to leave, with a trace saying
why.

## Do the work (Lean)

- Search before proving — the lemma may already exist. Skill: `leansearch`
  (`"$HORIZON_BIN" search` + the Lean LSP MCP search tools).
- Use the **Lean LSP MCP** for tight proof feedback, then verify with the narrowest
  faithful `lake` / `lake env lean` check. Skill: `lean-check`.
- When editing blueprint material, follow the house format. Skill: `blueprint-conventions`.

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

## Resuming after an interruption

State lives on disk, so a fresh session (even on another account) continues cheaply:

- **Why did the last run stop?** `"$HORIZON_BIN" usage --json` includes a
  `paused` field (from `runs/<id>/paused.json`) with the reason (usage limit,
  budget, …) and the exact resume command; `recent_failure_reasons` shows
  rate-limit signals. A paused/killed run resumes with
  `"$HORIZON_BIN" run --resume <id>`.
- **What was in flight?** Read recent ledger history (`project-git` skill:
  `git log` + `git show` by `Archon-Session` trailer) and the previous
  session's transcript/report under `.archon-horizon/runs/`.
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

There is **no separate Ground agent** running alongside you. *You* are the only
driver. When you judge the workspace needs tidying — the roadmap or memory has
drifted, the inbox is piling up, the blueprint↔Lean correspondence needs checking,
or you want a fresh-eyes review of what you just did — **spawn a subagent** to do
it, then carry on. This is a judgement call, not a schedule: clean up when it's
worth it, not on a timer. Available subagents (see the `subagents` skill):

- **janitor** — workspace hygiene: roadmap/READMEs concise, inbox from overflowing.
- **work-reviewer** — fresh-context review of your last work; is it converging?
- **blueprint** — Lean ↔ blueprint statement/`\uses` correctness for a scoped slice.
- **reference-retriever**, **debug**, **page-transcriber** — as needed.

Keep proving; delegate the upkeep.
