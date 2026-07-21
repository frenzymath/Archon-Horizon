---
name: subagents
description: How the Horizon agent delegates to native subagents for review and upkeep — spawning by name, read-only enforcement, dispatch-time model choice, and the dynamic descriptor roster.
---

Subagents are **your toolkit for delegation**: focused helpers you spawn to
divide up review and upkeep while you keep proving. The `ground` helper is the
fresh-context checkpoint for workspace-wide strategy and hygiene; it is a
subagent, not a second orchestrator role.

Each subagent is a single Markdown descriptor in
`.archon-horizon/subagents/<name>.md`. At the start of every `horizon run` those
descriptors are compiled into your engine's **native** subagent format,
workspace-local:

- Claude Code → `.claude/agents/<name>.md`
- Codex → `.codex/agents/<name>.toml`

List the descriptors under `.archon-horizon/subagents/` to see the current
roster; pick the right subagent and scope from their descriptions.

## Dispatch

Spawn a subagent **by name** through your engine's own native subagent
mechanism, with a focused directive: the slice/scope (a chapter, a task, a
project) and the project it applies to. Spawn several in parallel when the work
divides cleanly, then wait for and reconcile their reports. Give one subagent
one scoped slice so the workspace scales to many projects.

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
