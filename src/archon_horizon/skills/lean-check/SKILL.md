---
name: lean-check
description: Build and check Lean in this workspace and read the errors — use Lean LSP MCP for fast proof-writing feedback, then run faithful kernel verification with `lake build` / `lake env lean`.
---

- During proof writing, prefer the Lean LSP MCP server for fast local feedback:
  inspect goals/diagnostics at the edited location before running a full build.
  This is the fastest loop for filling proofs.
- Treat LSP feedback as interactive guidance, not final verification. The
  faithful check is Lean's kernel via the project build: run the configured
  build command, `lake build <Module>`, or `lake env lean <file>` from the
  project root.
- Prefer the project's configured build command when validating a task. Otherwise
  use the narrowest faithful check that covers the changed file/module.
- Re-run after each change and repair failures you introduce.
- Read errors top-down — the first error often causes the cascade. For each,
  note the `file:line`, expected vs actual type, and remaining goal.
- A `sorry` left in place is an open obligation, not a success. Do not hide a
  hard obligation behind a new `sorry` unless a hint explicitly allows it.

## Heavy builds — actually wait for them

A cold `lake build` (or one that recompiles Mathlib) can take many minutes. The
most common way a session ends on a wrong conclusion is **not waiting for the
build to finish**:

- Do **not** assume you will be "automatically notified" or "re-invoked" when a
  background build completes — whether that happens depends entirely on the
  mechanism you used. If unsure, run the build in the **foreground** and block on
  it until it exits, then read the exit code.
- If you run it in the background, you MUST actively wait: poll its status/output
  until there is a definitive exit code (a monitor / until-loop, re-reading the
  log), and only then conclude. A background job you started but walked away from
  is *not* done.
- Scope the build to the narrowest faithful check (`lake build <Module>` or
  `lake env lean <file>`) to keep the wait short; reserve the full project build
  for final validation.
- Never write your final report — or `complete` an inbox item, or mark a task
  done — while a build that affects your conclusion is still running. If the
  engine cannot wait long enough, say so explicitly under `## Issues` and leave
  the next action clear, rather than implying the proof checked.
- Before marking a task or roadmap item done/blocked/rejected, add a concise
  closing comment with the exact check command, result, relevant files, and any
  caveat. If a task has `roadmap_refs`, make sure the linked roadmap items end
  with a consistent status and their own sync/closing comment.

## When the LSP dies on big import files

The Lean LSP loads the full environment for the file you open. On a file whose
imports pull in most of Mathlib (a broad `import Mathlib` umbrella, or a module
that transitively imports one), that environment is huge and the LSP server can
run out of memory or time out and die. This is an LSP limitation, **not** a
proof error:

- Do not treat an LSP crash/timeout on such a file as a failure of your proof.
  Fall back to the kernel check: `lake build <Module>` or `lake env lean <file>`,
  which verifies faithfully without holding the whole environment interactively.
- Prefer opening the **narrowest** module in the LSP. Avoid pointing the LSP at
  umbrella/`import Mathlib`-style files just to get feedback; edit and inspect
  the specific module you are proving in instead.
- If the LSP has crashed, restart it (or just proceed with `lake build`) rather
  than repeatedly reopening the same giant file and getting the same crash.
