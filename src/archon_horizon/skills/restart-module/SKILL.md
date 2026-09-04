---
name: restart-module
description: >-
  Decide when layered Lean (or blueprint) debt needs a clean rewrite instead of
  more local patches — backup the cone, rebuild a simpler API, drop backups once
  git history holds them. Prefer restart over endless fix loops.
recommendation: >-
  Consider this skill when the same set_option/heartbeat or instance-depth
  failure recurs, when horizon benchmark ranks a file highly, or when fixing one
  layer keeps forcing new reducible/abbrev bridges. Not a completion gate.
---

# Restart a module (clean rewrite)

Horizon should not fear refactoring. A **clean rewrite of a bad cone** is often
cheaper and more correct than another session of local patches on layered
abbreviations, instance packages, and `set_option` workarounds.

## When to restart instead of patch

Prefer a restart when several of these hold:

- The same elaboration / synthesis / equivalence failure returns after two or
  more fix attempts (AI looping on the same blocker).
- The file (or its import cone) ranks high on
  `"$HORIZON_BIN" benchmark -p <project> --json` — large summed
  `set_option maxHeartbeats` / `synthInstance.maxHeartbeats` budgets.
- Public API is a stack of needless `abbrev`s, index types, and
  `@[reducible]` transport lemmas that only exist because of the packaging.
- **Core definitions** fail [[definition-quality]] criteria: several independent
  consumers share the same encoding pain (dispatch
  `definition-quality-reviewer` when unsure).
- Naming and module layout diverge from [[mathlib-conventions]] enough that
  readers cannot reconstruct the statement from the name.
- The **blueprint route** and the Lean route disagree, and aligning them would
  rewrite most of the file anyway ([[blueprint-conventions]],
  [[strategy-convergence]]).

Do **not** restart a small, isolated lemma that only needs a correct proof.

## Depth diagnosis (do this first)

1. Run `"$HORIZON_BIN" benchmark -p <project> --details --json` and note the
   top files and which options they raise.
2. List the public API surface consumers import (search with [[leansearch]]).
3. Draw the dependency cone: which files must move together? Prefer the
   **smallest** cone that removes the layered debt.
4. State the target design in one short paragraph (mathlib-shaped names,
   flat structure, no decorative abbrevs). Update the **blueprint first** if
   the mathematical route changes — Lean follows the blueprint, not the reverse.

## Backup, then rewrite

1. **Commit** any good work already in the tree (`project-git`).
2. **Backup** the cone before deleting or gutting it:
   - Preferred: `"$HORIZON_BIN" attempt save <files...> --reason "restart: <why>"`
     so the session dashboard keeps the dead end.
   - Also fine: copy under a short-lived path such as
     `.../Archive/Restart-<date>/` or a `*.lean.bak` next to the module **only**
     for the rewrite window.
3. Rewrite the module with a clean API:
   - Mathlib naming and module docstrings ([[mathlib-conventions]]).
   - Prefer inline types over one-off `abbrev` wrappers that force equivalence
     boilerplate.
   - Keep instance and morphism packaging shallow; one bundling layer beats three.
   - No new `set_option maxHeartbeats` unless a **measured** obligation remains
     after the redesign; treat remaining overrides as debt to file.
4. Point imports at the new surface; delete or stop exporting the old names.
5. Verify with [[lean-check]] (LSP loop, then `"$HORIZON_BIN" check`).
6. **Drop backups** once the new module builds and consumers are updated —
   git history already retains the old text. Do not leave permanent `*.bak`
   trees in the project.

## Blueprint and roadmap

- If strategy changed, refactor the blueprint nodes/proofs **before** or **with**
  the Lean rewrite so the blueprint remains the first source of truth.
- Record the restart on the roadmap (`kind: refactor` is appropriate) and leave
  a short inbox `memory` when the failure mode is reusable.

## Related

- [[lean-quality]] and `lean-quality-reviewer` — detect `set_option` and layering smells.
- [[api-composition]] — shallow packaging and bridges.
- [[janitor]] via writable layout moves after the API stabilizes.
- [[attempt]] / `horizon attempt save` — preserve rejected cones without cluttering HEAD.
