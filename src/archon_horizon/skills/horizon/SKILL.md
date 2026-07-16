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

- **Roadmap** — the strategy + progress map across *all* projects. Read it to see
  where things are going and what other projects have done; keep your own item's
  status/strategy current as you work. `"$HORIZON_BIN" roadmap …`
- **Tasks** — a specific piece of work, usually the one a human launched and is
  watching. `"$HORIZON_BIN" task …`
- **Inbox** — how agents talk across sessions (and projects). Leave a note for the
  next session; read what past ones left. `"$HORIZON_BIN" inbox …` (skill: `horizon-inbox`)
- **Blueprint DAG** — declaration dependencies and what's proved. `"$HORIZON_BIN" leandag …` (skill: `leandag`)
- **Memory** — durable facts/dead-ends: `.archon-horizon/memory.md`.
- **Usage** — your token/cost consumption this session and run, plus any
  configured budget headroom and recent rate-limit signals:
  `"$HORIZON_BIN" usage --json`. **Check it before fanning out subagents or
  starting very heavy work** — running out mid-proof loses more than pacing does.

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
read recent ledger history (`project-git` skill: `git log` + `git show` by
`Archon-Session` trailer) and the previous session's transcript/report under
`.archon-horizon/runs/` to see what was in flight, then pick up from there.

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
- **blueprint-reviewer** / blueprint checks — Lean ↔ blueprint statement/`\uses` correctness.
- **reference-retriever**, **debug**, **page-transcriber** — as needed.

Keep proving; delegate the upkeep.
