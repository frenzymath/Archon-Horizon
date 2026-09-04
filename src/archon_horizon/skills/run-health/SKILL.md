---
name: run-health
description: >-
  Lightweight operational checks for Horizon runs: task and roadmap health,
  inbox attention, live sessions, resource contention, version drift, and safe
  recovery boundaries.
---

# Run health

Use this lens when the CLI reports a warning, a run is long-lived, or work is
being coordinated across projects. It is operational triage, not a substitute
for mathematical or Lean review.

## Check the live state

Inspect protections and unread conversations first, then check:

- queued/running tasks, orphaned sessions, repeated retries, and queue size;
- roadmap parent/child status and duplicate or shared task references;
- inbox memory/info growth, stale conversations, and ownership/audience;
- active runs, build/index locks, detached processes, and resource pressure;
- workspace-local scratch pressure under `.archon-horizon/tmp/`, including stale
  per-session trees left by crashed engines;
- Horizon managed-file/version drift, rate-limit or authentication signals, and
  failed integrations;
- whether generated graph/cache changes are expected or need a coordinated
  maintenance window.

Use the narrowest CLI command with `--json` where possible and compare before
and after counts. A warning may be intentional; record the reason instead of
silently clearing it. Do not run destructive cleanup, `init --update`, broad
reindexing, or history repair while another live run owns the state unless the
owner explicitly coordinates it.

## Scratch pressure

Agent subprocesses should receive `TMPDIR`/`TMP`/`TEMP` pointing at
`$ARCHON_HORIZON_TMP`, below `.archon-horizon/tmp/`, rather than the shared
system `/tmp`. Treat that directory as disposable: durable evidence belongs in
the session artifacts, reports, or ledger. At a routine janitor checkpoint, run
`"$HORIZON_BIN" tmp clean --older-than-hours 24 --json` first. If the candidates
are stale, run the same command with `--apply`; live run markers are protected by
the command. A quota incident may justify a shorter age after confirming that no
active run's directory is being targeted. Never recursively delete the whole
state directory or another run's live scratch.

## Handoff

Report the snapshot, warning, safe action, remaining risk, and owner. Use
`janitor` for docs/inbox/roadmap tidying, `debug` for setup failures, and
[[provenance-isolation]] for cross-project or commit boundaries. Keep this
targeted response separate from optional semantic review.
