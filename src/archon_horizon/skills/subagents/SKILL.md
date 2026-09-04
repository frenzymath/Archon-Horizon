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

The roster is a set of options, not a hidden pipeline. The Horizon agent decides
whether a helper is worth the time, may use a different tool or create a
workspace-specific helper, and should say when a review was skipped. For
source-backed mathematics, [[formalization-review]] supplies shared questions;
`blueprint` is the writable scoped source/Lean owner and `work-reviewer` is the
fresh progress-integrity reviewer (objective, diff, report, and history). Use
`source-fidelity-reviewer`, `mathematical-correctness-reviewer`,
`blueprint-integrity-reviewer`, `proof-load-bearing-reviewer`,
`consumer-dependency-reviewer`, `api-composition-reviewer`,
`verification-integrity-reviewer`, `graph-traceability-reviewer`,
`provenance-integration-reviewer`, `strategy-reviewer`, or
`run-health-reviewer` for their named lanes. Use `lean-quality-reviewer` for
proof/code hygiene and downstream compatibility. Use
`definition-quality-reviewer` when the lead doubts a **core definition**: it
judges defs by how theorems/lemmas consume them and by Mathlib-shaped criteria
(`definition-quality` skill), rather than only polishing consumer proofs. Use
`honesty-reviewer` when a new certificate, target-shaped structure, completion
claim, or repeated route needs an explicit anti-evasion audit: classify
conditional versus proved producers, test vacuity/packaging, and compare the
source-facing frontier across rounds. This is the quickest way to catch a
compiling sequence that is merely renaming or wrapping the same gap. `ground`
keeps the workspace-level view without duplicating their scoped audits. For less
frequent claims, `external-boundary-reviewer`, `transcription-fidelity-reviewer`,
`release-reproducibility-reviewer`, and `review-adjudicator` are available as
focused opt-in profiles.

`default_enabled` on a descriptor controls whether its native definition is
compiled during workspace setup; it does not dispatch the helper or require a
review. A compiled reviewer remains dormant until the Horizon agent chooses it.

## Dispatch

Spawn a subagent **by name** through your engine's own native subagent
mechanism, with a focused directive: the slice/scope (a chapter, a task, a
project) and the project it applies to. Spawn several in parallel when the work
divides cleanly, then wait for and reconcile their reports. Give one subagent
one scoped slice so the workspace scales to many projects.

Delegation is part of the normal workflow, not an exceptional recovery path.
For a session that touches more than one file, more than one proof obligation,
or is expected to run longer than ten minutes, consider dispatching at least one
bounded review/helper before the final report when that would improve
confidence. For a new certificate or target-shaped wrapper, prefer
`honesty-reviewer`; for a repeated task or unchanged frontier, prefer it plus
`strategy-reviewer`/`work-reviewer`. On a multi-session task, consider `ground`
before the terminal completion claim and `janitor` at the scheduled hygiene
checkpoints. Use the engine's native `spawn_agent`/Task mechanism, wait for a
chosen helper to finish, and reconcile its report in your own result. These are
advisory signals, not runtime requirements; if no review is useful or
available, state why delegation was skipped in the report.

The exception is a live collection-health warning called out by the `horizon`
skill: use the targeted `janitor`/`ground` response, or record why it cannot be
run. That operational safeguard does not imply that every task needs a semantic
review helper.

Every descriptor invocation receives an isolated `$ARCHON_HORIZON_TMP` with
`TMPDIR`/`TMP`/`TEMP` pointed at it. Use that path for disposable downloads and
probes, and move anything worth keeping into the report, an attempt artifact,
or the ledger. The directory is normally removed when the helper exits. If a
crash leaves stale trees, ask `janitor` to inspect `"$HORIZON_BIN" tmp clean
--older-than-hours 24 --json`; cleanup must use the command's explicit
`--apply`, which protects live runs.

## Read-only and write scope

A descriptor's `read_only: true` is compiled to real engine enforcement — Claude
`disallowedTools: [Edit, Write, NotebookEdit]`, Codex `sandbox_mode = "read-only"`
— so a read-only reviewer cannot edit source. Every subagent (read-only or not)
can still file issues/memory and write its report via the `horizon inbox` CLI;
that is how read-only agents act. The descriptor's `write_domain` documents what
a writer is expected to touch (Lean/blueprint/reference source).

Writable helpers such as **`janitor`**, **`blueprint`**, and **`debug`** are
intentionally **not** read-only: janitor may restructure Lean/blueprint trees and
fix import paths; blueprint owns scoped `.tex`; debug may fix toolchain files.
Reviewers stay read-only. Do not mark a writer `read_only: true` "to be safe" —
that disables the layout and authoring work they exist to do.

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
