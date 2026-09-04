---
name: lean-quality
description: >-
  Review Lean implementation quality: proof robustness, diagnostics, imports,
  set_option/heartbeat blockers, shallow API packaging, naming, documentation,
  layout, and downstream compatibility without changing mathematical meaning.
---

# Lean quality

Use this lane for a proof-heavy change, cleanup, or exported module after its
statement has been judged separately. Quality is about maintainability and
composition, not about making a weak statement compile.

## Inspect

- Prefer stable, readable proof steps and reusable lemmas over brittle chains
  that depend on accidental reducibility, `change`, or opaque automation.
- Check warnings, linter output, unused variables/imports, unnecessary
  `classical`, **options**, suppressed errors.
- **Heartbeat / resource overrides are a first-class smell.** Scan for
  `set_option maxHeartbeats`, `set_option synthInstance.maxHeartbeats`,
  `maxRecDepth`, and similar. Run
  `"$HORIZON_BIN" benchmark -p <project> --details --json` (or the dashboard
  Benchmark view) to rank files by total raised budgets. High ranks usually
  mean deep instance search, needless `abbrev` layers, or bad abstraction —
  not a reason to raise the limit further without a redesign note.
- Check names, namespaces, **module placement / tree shape**, docstrings
  ([[mathlib-conventions]]), cross-references, deprecations, attributes, and
  line/resource hygiene.
- Look for **packaging depth**: index `abbrev`s for simple sums/products,
  towers of equivalent bundled types, global instances that create diamonds,
  and `@[reducible]` lemma farms that only repair definitional equality lost
  to those layers ([[api-composition]]).
- Look for dead helpers and duplicated proof code, but distinguish a deliberate
  local adapter from an accidental second public API.
- Build representative downstream users after an exported change and report
  the exact scope; a local file check is not a downstream compatibility check.

## Fix vs restart

Separate mechanical cleanup from architectural redesign. Preserve a verified
statement and dependency cone while suggesting quality improvements.

When the same blocker recurs, when heartbeat budgets dominate a file, or when
fixes only add more transport lemmas, first ask whether a **core definition** is
at fault ([[definition-quality]], `definition-quality-reviewer` — judge via
witness consumers). Then prefer [[restart-module]] when redesign is confirmed:
backup the cone (`horizon attempt save` / short-lived archive), rewrite a
thinner API, then drop backups once git holds history. Raising `maxHeartbeats`
alone is not a fix.

## Report

Give the revision, files and consumers inspected, diagnostics/commands
(including benchmark ranks when relevant), exact locations, consequence,
smallest compatible action **or** restart recommendation, unchecked scope, and
confidence. Classify findings as `confirmed`, `partial`, `unverified`, or
`no-issue`; do not mark a task complete or edit accepted source from a
read-only reviewer role.
