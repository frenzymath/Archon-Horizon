---
name: task-status
description: What the task status labels mean and how you (the agent) set them — you OWN your task's terminal status via `horizon task set`; the machine only ever leaves it queued/running, so `done` is never inferred from your report or a clean exit.
---

A task is the unit of work a run dispatches. **You own its outcome.** The
orchestrator/scheduler only ever writes `queued` and `running`; it never reads
your report and never marks a task `done`, `blocked`, or `failed` for you. If you
do not record a terminal status, the task simply returns to `queued` for the next
round. Record status through the CLI — invoke it via the `$HORIZON_BIN` env var
(an absolute path); a bare `horizon` may not be on your shell's PATH.

## The labels

- `queued` — runnable. The scheduler picks up only queued tasks. This is where a
  task rests between rounds when work remains. **Set by the machine (and humans);
  you normally don't.**
- `running` — a session is actively driving it right now. **Set by the machine at
  dispatch.** Don't set this yourself — it means "another session is live," and
  the CLI refuses to start a second run of a `running` task.
- `done` — the assigned objective is **FULLY complete**: the Lean is proved and
  builds, the blueprint node(s) are closed, nothing in the objective remains. Not
  "mostly done", not "the hard part is done", not "a clean commit landed". **Only
  you can set this, and only when it is genuinely, completely finished.** A
  reviewer (human or work-reviewer subagent) checks it against the diff and the
  Lean state and will flip a premature `done` back — so a false `done` wastes a
  round, it doesn't save one.
- `blocked` — you cannot make progress: a dependency is missing, a decision is
  needed, or an external constraint stops you. Say why in a comment.
- `failed` — you genuinely could not do the work (not merely "ran out of time" —
  that's just `queued` for next round). Say why in a comment.
- `cancelled` — abandoned by a human. You won't normally set this.

## How to set status

```
"$HORIZON_BIN" task set <task_id> --status done      # objective FULLY complete
"$HORIZON_BIN" task set <task_id> --status blocked   # stuck; explain in a comment
"$HORIZON_BIN" task set <task_id> --status failed    # could not do it; explain
```

Authorship is automatic — the CLI stamps your role (normally `horizon`) from the
run environment; do not pass `--author`. Setting a status appends an auditable
history entry, and if the task has `roadmap_refs` the linked roadmap items are
synced (done→done, blocked/failed→blocked, cancelled→rejected).

Task commands also report advisory queue-health warnings. Review an oversized
open queue for duplicated or roadmap-only objectives, and investigate a task
that has remained `running` past the stale-status window. These warnings never
change status for you; if the state is intentional, it may remain as-is.

## Comment as you work

Leave a short comment only for a durable state change or blocker. Default to one
sentence or at most three bullets: conclusion, evidence, next open obligation.
Do not restate commit summaries, the session report, or earlier comments.

```
"$HORIZON_BIN" task comment <task_id> --body "Closed the base case; representability remains open."
```

## The one rule to remember

**If the objective is only partly advanced, set nothing.** The task returns to
`queued` and the next round continues it. Reserve `done` for work that is
completely finished — that is the entire point of the status now living with you
instead of being guessed from your report.
