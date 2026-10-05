---
name: lean-check
description: Choose and interpret Lean diagnostics, managed builds, axiom checks, and publication evidence for the exact changed source without duplicating builds or overstating proof completion.
metadata:
  category: lean
---

# Check Lean With Traceable Evidence

Locate the Lean project root; it may be below the workspace root. Read
`lean-toolchain`, Lake targets, dependency pins, and changed imports. Preserve
these unless modifying them is part of the work.

Use available LSP diagnostics and goal inspection during the edit loop.
Refresh them after edits; a diagnostic for the old document is stale. At a
publication boundary or after exported/import changes, choose affected modules
and relevant consumers. Run the repository's required root targets for a
release. Do not run a whole-project build after every tactic experiment.

When `HORIZON_LEAN_BUILD` is configured, use the installed foreground helper
from the project root:

```sh
horizon-lean-check MyProject.Changed
horizon-lean-check --lean MyProject/Changed.lean
```

The module form prepares dependency outputs. The file form requires its imports
already built. Omit targets for default Lake targets; `--root PATH` selects
another checkout and `--probe` checks readiness without compiling. The module
entrypoint is `python -m archon_horizon.pipeline.worker.lean_build`. Without a
managed policy, use the project's ordinary Lake commands. Do not bypass host
admission to evade a busy build slot.

Read the JSON result, not just the exit code. Exit 75 is deferred, not failed
mathematics or successful verification. Exit 124 is timeout. Preserve useful
source and arrange an owned later check when deferred; repeat only after inputs
or availability change. A deterministic source error needs a repair, and a
transport/cache failure needs diagnosis. `snapshot_verified: false` cannot
support a claim about one exact stable source snapshot.

Use pinned dependency caches when available. Do not run `lake update`, delete
shared cache directories, or change toolchains simply to cure a cache miss.
Check readiness and the actual failing dependency first. Never report a cache
hit as evidence for changed inputs without establishing their identity.

Record source identity, command, checked targets, exit status, and concise
diagnostics. LSP-only progress or a skeleton can be published honestly without
claiming a full build. A build can succeed with admissions; use
[proof-review](../../review/proof-review/SKILL.md) when claiming proof closure.
Confirm that required files and checks correspond to the published revision,
and let [horizon-pipeline](../../operations/horizon-pipeline/SKILL.md) handle
publication receipts and durable unfinished obligations.
