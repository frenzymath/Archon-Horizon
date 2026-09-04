---
name: verification-evidence
description: Calibrate formalization claims against the exact Lean diagnostics, build target, artifacts, dependency closure, warnings, and axiom or sorry evidence that were actually obtained.
---

# Verification evidence

Use this lens whenever a report says checked, fixed, complete, axiom-clean,
sorry-free, or required. Verification is layered; never let one layer stand in
for another.

## Evidence ladder

Record separately:

1. source scans and text searches (useful inventory, not proof);
2. LSP diagnostics and goals (fast interactive feedback, not final closure);
3. `lake env lean <file>` or a module target (the named file actually elaborates);
4. the configured project/root build (the intended import closure builds);
5. dependent or consumer builds (exported interfaces still compose);
6. artifacts such as `.olean`, traces, and generated graph status;
7. direct and transitive `sorry`/axiom evidence, including `#print axioms`.

Name the command, working directory, revision, exit status, and relevant output.
Check that a tracked file is reachable from the target: a green root build can
omit a broken, root-unreachable module. Conversely, a source scan or graph
`lean_ok` label is not kernel evidence. Watch for stale trace-without-olean
pairs, interrupted child processes, warning floods, and concurrent build races.

## Calibrate claims

Use precise language: `diagnostics clean`, `target elaborates`, `root build
passes`, `consumer checked`, `direct axioms observed`, or `unverified`. Do not
collapse these into "fully verified." If a check was skipped because of shared
resources, say so and identify the smallest next check.

Pair with [[lean-check]] for the normal edit loop, [[graph-traceability]] for
derived statuses, and [[review-method]] for a reproducible report.
