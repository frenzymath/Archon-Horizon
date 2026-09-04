---
name: horizon-start
description: Work out which situation you were launched into — first round of a fresh run, a hand-off from the horizon session that just finished, a resume after a crash or pause, or a session outside any run — and pull exactly the context that situation calls for. Load at the START of a session, before picking up the task.
---

Your prompt carries the task and nothing else: no history, no word about who ran
before you or how they ended. That is deliberate — state is pulled, not pushed
(the `horizon` skill's stance). But it means a session that starts working
immediately is guessing. Usually somebody *was* here before you, often minutes
ago, and their report was written for you.

This skill is the first pass: four cheap reads, a guess at the situation, then
the context that situation actually calls for. Budget a couple of minutes, not a
tenth of the session.

## The four signals

```bash
ls "$ARCHON_HORIZON_SESSION_DIR/.."     # sibling sessions in THIS run
"$HORIZON_BIN" usage --json             # `paused` (why the last run stopped) + `recent_failure_reasons`
"$HORIZON_BIN" ps                       # is another run live on this workspace RIGHT NOW?
"$HORIZON_GIT" log --oneline -15        # what actually landed recently
```

The sibling listing is the one that decides most of it. A run's sessions look
like:

```
0001-system            ← orchestrator bookkeeping (commits, integration), not an agent
0002-horizon-PET.5     ← a horizon session; meta.json + transcript.jsonl + usage.json
0003-system
0004-horizon-PET.5     ← ...you, probably
```

For any earlier `*-horizon-*` sibling, two files answer everything:

- **`report.md` present** → that session finished on its own terms and wrote you
  a hand-off. **`report.md` absent** → it was killed mid-flight. This is the
  single most informative bit on disk.
- **`meta.json`** → `round`, `task_id`, `status`, `started_at`, `ended_at`,
  `model`. `ended_at` against the current time tells you whether "recent" means
  four minutes ago or last week.

## Which situation am I in?

Work down the list; the first match is your best guess.

**An earlier horizon sibling has no `report.md`** → *resume after a crash or
kill.* Its work is half-done and possibly half-committed. Read its commits in
the ledger first (they are the durable result), then inspect any `attempts/`
artifacts it deliberately preserved, then the tail of its
transcript to see what it was in the middle of — `usage --json` `paused` gives
the reason it died (usage limit, budget, auth), and `recent_failure_reasons`
shows rate-limit pressure that may still be there. Trust the ledger over the
transcript: uncommitted work survives only when it was explicitly preserved
under `attempts/`. If the dead session shares your `ARCHON_HORIZON_TASK`, you are continuing
its exact work — pick up its front rather than opening a new one.

**An earlier horizon sibling has a `report.md`** → *hand-off from the session
that just finished.* The common case. Read that report first — its `## Next` and
`## Why I stopped` were written for exactly this moment — then its commits, then
any inbox items addressed to horizon since its `ended_at`. Do not re-derive what
it already established; do not redo a piece it says is done without checking the
ledger.

**`ARCHON_HORIZON_ROUND` is `0` and no horizon sibling exists** → *first agent
of the run.* Nobody handed you anything **in this run** — but the workspace is
almost never greenfield. Earlier runs live under `.archon-horizon/runs/`; the
roadmap, the inbox and the ledger carry their conclusions. Skim the roadmap and
recent ledger history before deciding a strategy that a previous run already
tried and rejected. If `roadmap list` is empty (or has no item covering this
task's objective), **draft the strategy outline before deep proof work** —
`roadmap add` nested goals for the source-facing frontier and the next producers,
then `task set … --roadmap-ref` so later sessions inherit the plan.

**`ARCHON_HORIZON_RUN` is unset** → *not launched by the orchestrator* (an
interactive or hand-driven session). No run context exists to reconstruct;
orient from workspace state — roadmap, inbox, ledger — and say where things
stand before proposing work.

One caveat about round numbers. `ARCHON_HORIZON_ROUND` is 0-based and reliable
for "am I first" — but a resumed run can re-run a round whose session died, so a
`0` with a dead sibling present means *resume*, not *fresh*. The dead-sibling
check settles it either way. (`ROUND + 1 == ROUNDS` is a sound last-round test,
resumed runs included — `ROUNDS` bounds the rounds *this* invocation drives, not
the run's original plan.)

## Then, before you start

- **`horizon ps` said another run is live?** You share one ledger branch with it.
  Commit in small coherent pieces and expect to see its commits interleaved.
- **Timestamps are context.** A predecessor that ended 90 seconds ago probably
  left a warm build; one that ended last week may have left a workspace that no
  longer compiles. Check rather than assume.
- **The inbox is the cross-session thread** — read what past sessions left for
  horizon, and the task's own comments if you share their `task_id`
  (`"$HORIZON_BIN" task show "$ARCHON_HORIZON_TASK" --json`). Skill:
  `horizon-inbox`.

Then stop orienting and start working. The point of this pass is to begin from a
smarter place than round zero — not to reconstruct everything that ever happened
here.
