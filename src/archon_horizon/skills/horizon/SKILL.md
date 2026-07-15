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

If a human launched this session on a specific task/target, start there.

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

## Delegate when it helps (optional)

You may spawn a subagent for review or upkeep (blueprint checks, janitor hygiene,
work review, reference retrieval) — see the `subagents` skill. It's a judgement
call, not a requirement.
