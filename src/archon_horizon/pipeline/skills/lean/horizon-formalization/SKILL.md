---
name: horizon-formalization
description: Develop or adapt Lean statements, definitions and proofs for the current Horizon task using its pinned sources and toolchain.
metadata:
  category: lean
---

# Work On The Mathematics

Start with the assigned statement, definitions, source and existing Lean code.
The mission and instructions define the deliverable. Read only the relevant
part of the [phase workflow](../../operations/horizon-pipeline/references/phases.md)
when that boundary is unclear.

| Phase | Usual deliverable |
| --- | --- |
| Preprocessing | Roadmap statements and dependencies, faithful Lean theorem contracts and concrete supporting definitions in reviewed PRs. Proof admissions remain explicit. |
| Formalization | Proofs and useful helpers in the workspace against the adopted baseline. Graph changes are reviewed; ordinary proof edits are not library PRs. |
| Postprocessing | A coherent result family adapted into the library, normally with the existing proofs, submitted for review. |

Preprocessing ends at its accepted deliverable; human approval for a later proof
run does not require keeping its sessions alive. During formalization, public
contract corrections require the configured review and explicit adoption of
the replacement baseline. Postprocessing needs a separate statement prototype
only when an unresolved interface would otherwise cause substantial rework.
It does not repeat preprocessing for every existing proof.

Search the assigned project and pinned dependencies before constructing new
infrastructure. Check candidate signatures and versions. For remote indexed
search, inspect the returned repository revision. A result in another toolchain
is useful evidence, not proof that the declaration is available here.

Preserve the mathematical target. Do not weaken it, assume the desired result,
or move an unproved obligation into a certificate field. Keep remaining
admissions and assumptions visible. A successful build does not establish
source fidelity or close the proof's transitive dependency chain.

## Check The Changed Work

Use language-server diagnostics for the local edit loop. Check affected modules
and consumers at publication boundaries; reuse valid receipts instead of
rebuilding unchanged source. When `HORIZON_LEAN_BUILD` is present, use
`horizon-lean-check MyProject.Module` (or the corresponding
`python -m archon_horizon.pipeline.worker.lean_build` command). Direct
`lake build` must not bypass managed admission.

The helper emits JSON. Exit 75 is deferred, not verified; preserve progress and
arrange a later check. Exit 124 is a timeout. Repair the cause before repeating
the same failed build. `--probe` checks readiness; `--lean File.lean` checks a
file without building its imports. Use compatible precompiled dependency caches.
A result with `snapshot_verified: false` is not an exact-source receipt.

Keep edits within assigned ownership and publish useful progress before
yielding. For review repairs, work from the concrete findings and preserve
unchanged source and evidence. Return changed declarations, checked revision,
validation scope and any remaining mathematical gap.

Use a supporting procedure only when it answers the current question:
[lean-search](../lean-search/SKILL.md) for existing interfaces,
[lean-cycle](../lean-cycle/SKILL.md) for sustained proof work,
[lean-proof-repair](../lean-proof-repair/SKILL.md) for diagnostics,
[lean-check](../lean-check/SKILL.md) for validation, and
[lean-library-engineering](../lean-library-engineering/SKILL.md) for library
adaptation. A prepared reviewer follows its assigned rubric; it need not load
this skill or every Lean procedure to publish a review.
