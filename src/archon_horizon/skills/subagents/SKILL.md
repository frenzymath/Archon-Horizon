---
name: subagents
description: How the Horizon agent delegates to native subagents for review and upkeep — spawning by name, read-only enforcement, dispatch-time model choice, and the dynamic descriptor roster.
---

Subagents are **your team's workers**: focused helpers you (the team lead) spawn
to divide up review and upkeep while you keep proving. This is delegation
*within* your team and needs no permission — distinct from launching new tasks or
runs for *other* teams, which is off by default (see the `horizon` skill,
"Delegating beyond your team"). The `ground` helper is the fresh-context
checkpoint for workspace-wide strategy and hygiene; it is a subagent, not a
second orchestrator role.

Bundled descriptors and any workspace overrides under
`.archon-horizon/subagents/<name>.md` are merged at the start of every `horizon
run`, then compiled into your engine's **native** subagent format,
workspace-local:

- Claude Code → `.claude/agents/<name>.md`
- Codex → `.codex/agents/<name>.toml`

Use the engine's native roster, or list `.codex/agents/*.toml` for Codex and
`.claude/agents/*.md` for Claude, to see every runnable helper. Do not treat an
empty `.archon-horizon/subagents/` directory as an empty roster: that directory
holds workspace overrides/custom helpers only; built-ins such as `ground`,
`janitor`, and `work-reviewer` still appear in the compiled native directory.
Pick the right helper and scope from those generated descriptions.

## Dispatch

Spawn a subagent **by name** through your engine's own native subagent
mechanism, with a focused directive: the slice/scope (a chapter, a task, a
project) and the project it applies to. Spawn several in parallel when the work
divides cleanly, then wait for and reconcile their reports. Give one subagent
one scoped slice so the workspace scales to many projects.

Delegation is part of the normal workflow, not an exceptional recovery path.
For a session that touches more than one file, more than one proof obligation,
or is expected to run longer than ten minutes, dispatch at least one bounded
review/helper before the final report. On a multi-session task, dispatch
`ground` before the terminal completion claim and `janitor` at the scheduled
hygiene checkpoints. Use the engine's native `spawn_agent`/Task mechanism,
wait for the helper to finish, and reconcile its report in your own result. If
no suitable helper is available, state why delegation was skipped in the report.

## Read-only and write scope

A descriptor's `read_only: true` is compiled to real engine enforcement — Claude
`disallowedTools: [Edit, Write, NotebookEdit]`, Codex `sandbox_mode = "read-only"`
— so a read-only reviewer cannot edit source. Every subagent (read-only or not)
can still file issues/memory and write its report via the `horizon inbox` CLI;
that is how read-only agents act. The descriptor's `write_domain` documents what
a writer is expected to touch (Lean/blueprint/reference source).

## Model and effort — the Horizon agent owns the spend

Descriptors intentionally carry no model, tier, or effort setting. Generated
Claude/Codex descriptors inherit the parent session by default. At dispatch time,
the Horizon agent chooses the native model and reasoning effort for that helper:
use the same model/effort for mathematical review, a lighter capable option for
mechanical search/lint/hygiene, and a vision-capable option for page transcription.
If the engine cannot express a per-call override, inherit the parent and record
the choice in the report rather than hiding a model policy in `config.yaml`.

The same rule applies to a named subagent, a bare Task/Agent spawn, or any
workflow fan-out. Match model and effort to the actual slice, and before a large
fan-out check `"$HORIZON_BIN" usage --json`. If headroom is low, commit work in
flight first so an interruption loses nothing.

To add or change a subagent, edit its descriptor under
`.archon-horizon/subagents/` — it recompiles on the next run. Do not rely on a
stale hard-coded roster in this skill.
