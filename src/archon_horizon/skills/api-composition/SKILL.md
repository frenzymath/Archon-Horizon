---
name: api-composition
description: >-
  Review Lean API design for reuse and composition: existing abstractions,
  duplicate concepts, bridges, bundled context, instances, names, and
  downstream consumers.
---

# API composition

Use this lens before accepting a new public definition, wrapper, instance, or
bridge. Search the whole workspace and mathlib with [[leansearch]] or
[[mathlib-orientation]] before assuming an API is absent.

## Questions to ask

- Is this genuinely new, or a second spelling/packaging of an existing concept?
- Is the public statement at the right abstraction level, with a local model
  clearly named as such?
- If two representations should compose, is there an equality, `Iff`,
  equivalence, naturality, or transport theorem with explicit hypotheses?
- Are dependent maps, rings, scalar towers, covers, and instances bundled once
  and reused, or reconstructed at each consumer so definitional equality fails?
- Does an instance introduce a diamond, unexpected synthesis path, or global
  assumption? Would a named value be safer?
- Are names discoverable, docs cross-references valid, imports minimal, and
  deprecated APIs avoided?
- Is there a real downstream consumer, or is the declaration only activity
  around an unconnected scaffold?
- **Thin packaging:** does an `abbrev` exist only to name a simple sum/product
  or index, forcing `@[reducible]` transport lemmas and slower inference? Prefer
  writing the type at use sites or one real structure. Count instance/package
  layers — more than one or two bundling layers is often a redesign smell
  ([[restart-module]], `"$HORIZON_BIN" benchmark`).
- **Definition root cause:** when several consumers of the same `def` struggle
  the same way, audit the **definition** with [[definition-quality]] (and
  optionally `definition-quality-reviewer`) before blaming each proof.

Prefer a general reusable lemma over a local tactic-shaped helper. Keep a
faithful source-facing API and a computational/proof adapter separate, then
bridge them explicitly. A shorter proof is not a reason to weaken or duplicate
the interface.

## Evidence

Show search queries, declaration locations, import closure, instance behavior,
and at least one meaningful consumer when claiming composition. Check a
downstream target after exported changes. Report duplicate, missing bridge,
context drift, dead API, and naming/documentation defects separately; they may
need different owners. Pair with [[definition-quality]], [[load-bearing]],
[[lean-quality]], and [[verification-evidence]].
