---
name: lean-check
description: Build and check Lean in this workspace and read the errors — use Lean LSP MCP for fast proof-writing feedback, then run faithful kernel verification with `lake build` / `lake env lean`.
recommendation: >-
  When a bounded build of a large Lean declaration times out or produces no
  artifact, consider an in-place stub/body-isolation probe on the working Lean
  file: preserve the current proof in comments, activate a typed `by sorry`,
  and progressively restore proof segments while separating signature,
  body, and downstream checks. Restore clean sorry-free source before accepting
  any result.
---

## Required proof loop

Load this skill before editing a Lean file. Before the first proof edit, invoke
at least one Lean LSP MCP query for the target (`lean_diagnostic_messages` or
`lean_goal`). After every proof edit, invoke diagnostics or the goal query again
before starting another tactic attempt. Use `lean_multi_attempt`, hover, and
search to investigate candidates instead of repeatedly launching a build.

Do not start iterative proof work with `lake build`: it is a slow, final-boundary
check. Use LSP for the edit loop and reserve `lake build` for the final session
validation (or when a kernel check is specifically needed). When LSP is
unavailable or crashes, record that in the report and use the narrowest fallback,
usually `lake env lean <file>` or a single module, rather than silently skipping
the check.

LSP is sufficient between edits and between separate obligations. Do not run a
module build after every proof or repeat a narrow build immediately before a
configured final build that covers the same files. At the final boundary, run
the configured build once and wait for it. Add an earlier narrow kernel check
only when LSP failed, the change crosses an interface LSP cannot validate, or a
specific elaboration/kernel question must be settled before continuing.

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

Kernel/build evidence answers whether Lean accepts the checked target; it does
not establish that a source-backed declaration has the intended meaning. Pair
the verification lens with [[formalization-review]] when the task makes a
mathematical correspondence or completeness claim.

## Fast LSP proving loop

The `lean-lsp` MCP server gives sub-second feedback — far faster than a build
(10–30s+). Drive proofs with it, then confirm with the kernel. Key tools:

- `lean_goal(file, line)` — the proof state at a line. Call before writing any
  tactic and after each one. Empty `goals_after` = that step closes the goal.
- `lean_diagnostic_messages(file)` — instant errors/warnings after every edit
  (`[]` = no errors, but *not* proof-complete — confirm with `lean_goal`).
- `lean_multi_attempt(file, line, snippets=[...])` — test 3–5 **single-line,
  indented** candidate tactics at once and see exactly which close the goal and
  why the others fail. The highest-leverage tool for filling a `sorry`.
- `lean_hover_info(file, line, col)` — type/signature/docs at an identifier
  (point at the first char). `lean_file_outline(file)` — declarations + line
  numbers without reading the whole file. `lean_run_code("#eval …")` — quick
  standalone `#check`/`#eval`/`#print`.

Core loop: `lean_goal` (what to prove) → search a premise (see the `leansearch`
skill: `lean_local_search` / `lean_leansearch`) → `lean_multi_attempt` (test
candidates) → edit with the winner → `lean_diagnostic_messages` (confirm) →
kernel-check the module at the end.

## Heavy builds — actually wait for them

Read the WHOLE build output, not just pass/fail: warnings (unused variables,
deprecations, `sorry` notices, unexpected recompiles) are actionable — fix the
ones your change introduced, and record pre-existing/toolchain ones as memory
or an inbox issue rather than scrolling past (see the `horizon` skill,
"Warnings are work").

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

## Heavy declaration isolation (advisory)

When a narrow build times out without diagnostics or an `.olean`, it can be
useful to treat that as an elaboration-localization problem rather than as a
proof failure. One possible probe is an **in-place** edit of the working Lean
file, after recording the current diff or rejected attempt so it can be
restored. Where the declaration signature can remain intact, preserve the
original proof as a clearly delimited comment and activate a typed `by sorry`
in its place. A bounded build with that stub can distinguish an expensive proof
body from a costly declaration type, imports, implicit arguments, or dependent
instances. If consumers fail after the stub builds, the exposed interface may
be insufficient.

For a useful bisection, consider progressively uncommenting or de-sorrying one
proof segment, field, map, or equality at a time in the same file, checking the
smallest faithful target after each change. Temporary `sorry`/axiom edits are
diagnostic evidence only; record them as rejected attempts and restore clean
accepted source. A clean configured build and axiom audit are useful final
evidence before accepting a result.
