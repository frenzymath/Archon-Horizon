---
name: subagents
description: How the Horizon agent delegates to native subagents to divide review/upkeep work — spawning by name, read-only enforcement, model tiers, and the dynamic descriptor roster.
---

Subagents are **your toolkit for delegation**: focused helpers you spawn to
divide up review and upkeep while you keep proving. There is no separate Ground
agent — when you judge the work needs tidying or a fresh-eyes check (see the
`horizon` skill), spawn the right subagent yourself, then carry on.

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

## Model tier

A descriptor selects its model engine-agnostically: `tier: small|medium|big`
(resolved per-harness from that harness's `models:` map) or an explicit `model:`
override. When neither is set, the subagent inherits the parent session's model
— which keeps provider routing intact. Prefer a cheaper `tier` for mechanical
checks.

To add or change a subagent, edit its descriptor under
`.archon-horizon/subagents/` — it recompiles on the next run. Do not rely on a
stale hard-coded roster in this skill.
