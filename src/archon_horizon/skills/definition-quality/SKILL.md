---
name: definition-quality
description: >-
  Mathlib-based criteria for good Lean definitions, and how to detect bad ones
  from the theorems/lemmas that consume them — suboptimal consumers often mean
  a bad underlying definition, not only a hard proof.
recommendation: >-
  Consider this skill when proofs around a definition need heartbeats, reducible
  transport farms, or repeated rewriting of the same concept; dispatch
  definition-quality-reviewer when the lead agent doubts a core def. Advisory,
  not a completion gate.
---

# Definition quality

Bad **definitions** are often the root cause of stuck formalization. Theorems
and lemmas that look “hard for no mathematical reason,” force
`set_option maxHeartbeats`, need `@[reducible]` equivalence farms, or rebuild
the same data at every call site usually inherit the problem from an underlying
`def` / `structure` / `class` / `abbrev` — not from the consumer’s proof skill
alone.

Use this lens when the lead agent **doubts a core definition**, before stacking
more lemmas on it. Pair with [[api-composition]] (surface/bridges),
[[lean-quality]] (heartbeats/hygiene), [[mathlib-conventions]] (names/docs),
[[load-bearing]] (phantom fields), and [[restart-module]] when the fix is a
clean redesign.

## What a good definition looks like (mathlib-shaped)

Authoritative upstream: [Library style](https://leanprover-community.github.io/contribute/style.html),
[Naming](https://leanprover-community.github.io/contribute/naming.html),
[Documentation](https://leanprover-community.github.io/contribute/doc.html),
and nearby Mathlib source under `.lake/packages/mathlib/`.

A strong definition tends to:

1. **Match the mathematics at the right abstraction.** Prefer the structure
   mathematicians and Mathlib already use (`LinearMap`, bundled `…Hom`,
   `Filter`/`Tendsto`, typeclasses with coherent instances) over a private
   encoding of the same idea.
2. **Be stated once, reused many times.** One canonical carrier; adapters and
   coordinate models are clearly named and **bridged**, not silently duplicated.
3. **Have useful definitional behaviour.** Unfolding should expose a form that
   `simp`/`rw`/`exact` can use. Avoid packing that forces every proof through
   non-definitional transport (`Equiv`, custom `≃`, hand-rolled `cast`).
4. **Keep bundling shallow.** Data that always travels together may live in one
   structure/class; do not stack package/index/`abbrev` layers that only name
   `A ⊕ B` or rebuild instances at each consumer.
5. **Carry honest hypotheses.** No hidden global assumptions in instances; no
   fields that restate the theorem being proved ([[load-bearing]], [[honesty]]).
6. **Be discoverable.** Mathlib-style name, module placement, and a faithful
   module/`/--` docstring that matches the actual type
   ([[mathlib-conventions]]).
7. **Have real consumers.** A public def with no theorem that needs it is
   activity, not infrastructure — unless it is an intentional staged API.

## What a bad definition looks like (symptoms)

Treat these as **definition suspects**, especially on core API:

| Smell in the definition | Typical consumer fallout |
|---|---|
| Needless `abbrev` for a simple sum/product/index | `@[reducible]` lemma farm; broken `defeq`; slow synthesis |
| Deep tower of equivalent bundled types | Heartbeats on every lemma; diamonds; “which instance?” |
| Coordinate/model object sold as the intrinsic one | Every theorem re-proves transport; blueprint drift |
| Duplicate of a Mathlib/project concept under a new name | Parallel APIs; search misses; double maintenance |
| Fields/hypotheses unused by any consumer | Scaffold / target-shaped packaging ([[load-bearing]]) |
| Unfolds to something proofs cannot use | Manual `change`/`congr`/`cast` everywhere |
| Instance that reconstructs context each time | `synthInstance.maxHeartbeats`; fragile typeclass search |
| Name/path that hides the math claim | Unsearchable; wrong generality |

Static cost signal: `"$HORIZON_BIN" benchmark -p <project> --json` on modules
that **define** the API and on modules that **only consume** it. When consumers
are hot but the math is routine, look **down** at the defs they import.

## Detect bad definitions from consumers (primary method)

Do **not** start by rewriting the def. Start from stuck or ugly **consumers**:

1. **Pick the painful theorems/lemmas** (failed goals, high heartbeats, huge
   proof terms, endless transport).
2. **List the definitions they elaborate against** — types of binders, instances
   synthesized, `abbrev`s unfolded, structures projected. Use Lean LSP hover,
   `#check`, `#print`, and go-to-definition; search call sites with
   [[leansearch]] / `grep` for the def name.
3. **Ask for each core def:** if this def were Mathlib-canonical and thin,
   would this proof still be painful? If several independent consumers share
   the same pain, the def is the common cause.
4. **Compare to Mathlib.** Search the same concept; open the Mathlib def and
   two lemmas that use it. Note argument order, bundling, and what is
   definitional there vs local.
5. **Probe thin alternatives (read-only or scratch).** In scratch or a private
   sketch: replace the suspect with a Mathlib type or a flatter structure and
   see whether a representative lemma simplifies. Do not claim success without
   a real check ([[lean-check]]).
6. **Separate layers.** Consumer proof skill vs API design vs missing bridge vs
   wrong theorem statement. Only the design layer belongs to this skill’s
   primary fix (often redesign the def, then adapt consumers).

## Criteria checklist (quick)

For a candidate definition `D`:

- [ ] Is there already a Mathlib or workspace def for this concept?
- [ ] Is the type the mathematical object, or an encoding/index/package?
- [ ] Do ≥2 independent consumers struggle in the same way?
- [ ] Do consumers need non-definitional transport that Mathlib analogues avoid?
- [ ] Does `"$HORIZON_BIN" benchmark` light up consumers of `D` more than peers?
- [ ] Would deleting an `abbrev`/layer and inlining the type remove lemmas?
- [ ] Are instances canonical and diamond-free?
- [ ] Does the docstring match the type; is the name reconstructible from the statement?

If several boxes fail, prefer redesigning `D` ([[restart-module]] when layered)
over proving one more lemma about it.

## Report shape

When auditing, name:

- **Suspect definitions** (module, name, kind: def/structure/class/abbrev).
- **Witness consumers** (theorems/lemmas and how they hurt).
- **Mathlib/workspace analogue** (or “none found” with query).
- **Mechanism** (defeq loss, instance depth, duplicate concept, …).
- **Smallest fix** (flatten abbrev, switch to Mathlib type, add one bridge,
  full module restart) and what must not change (source-facing statement).

Do not mark tasks done from a review-only pass. File inbox `issue`/`memory`
when the finding should outlive the session.

## Related

- Reviewer subagent: `definition-quality-reviewer` (dispatch when in doubt).
- [[api-composition]] — public surface and bridges once the def is sound.
- [[formalization-review]] — source shape vs proxy when literature-backed.
