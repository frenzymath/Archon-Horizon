---
name: honesty
description: Detect formalization and process shortcuts that make a result look proved without discharging the source obligation, including weak certificates, vacuity, hidden assumptions, stale evidence, and repeated route churn.
recommendation: >-
  consider this lane for any new certificate, target-shaped structure,
  `\leanok`/completion claim, or task that has repeated rounds. It is a
  focused integrity check, not a proof gate; record why it was skipped when
  the risk is material.
---

# Honesty and anti-evasion

This skill asks whether the artifact and the report say what they appear to
say. It does not require every difficult theorem to be finished, and it does
not treat a conditional interface as useless. The required distinction is
between a useful assumption, a proved producer, and a claim that merely moved
the obligation elsewhere.

## Certificate taxonomy

Classify every new certificate, context structure, wrapper, or instance before
calling it progress:

- **proved producer** — constructed from already checked data, with a
  declaration-level proof and a real consumer;
- **conditional interface** — a structure whose fields are explicit premises
  supplied by a caller. This is legitimate scaffolding, but it is not an
  existence theorem and must not be attached as an unconditional source claim;
- **imported boundary** — supplied by another module, project, or human/source
  decision. Record the exact import and trust boundary;
- **axiom/sorry-backed** — its transitive `#print axioms` contains `sorryAx`, a
  project axiom, or another non-approved trust primitive;
- **empty or vacuous** — a certificate has no fields, an empty index/domain
  makes the predicate automatic, or a default/zero witness satisfies every
  obligation without encoding the intended object;
- **unverified** — the declaration or its consumers were not checked at the
  revision being reported.

An empty `structure Certificate where` is inhabited by `⟨⟩`; it is not itself
an axiom or a `sorry`, but it carries no mathematical evidence. A theorem of the
form `foo (h : P) : P := h`, or a certificate whose only meaningful field is
the target conclusion, is **hypothesis packaging**: it assumes the obligation
instead of producing it. If that premise is introduced with `axiom` or `sorry`,
the target is transitively as untrusted as that premise. Never hide such a
boundary behind a friendly name, typeclass, `Nonempty`, `Exists`, or wrapper.

## Attachment and naming invariant

Name assumption packages explicitly (`*Assumptions`, `*Contract`, `*Scaffold`,
or `*Proxy`) and keep them separate from the source-facing theorem. A source
node may receive a `leanok`/completion attachment only from a producer or a
proved bridge whose dependency closure includes the source assumptions and
whose intended consumer is visible. A theorem that merely projects fields from
a caller-supplied package is a valid conditional API, but it is not the
producer and must not silently inherit the source node's completion status.

## Anti-evasion checks

For a changed declaration or completion claim, use the smallest useful probes:

1. Read the exact type and constructors. Which field contains new information,
   and which declaration constructs it? A projection, alias, `rfl` wrapper, or
   re-export is not a producer.
2. Follow one real consumer. Does it use the certificate's substantive fields,
   or merely carry the package and return a weaker/conditional result? Delete or
   replace a suspicious field in a temporary probe when practical.
3. Run declaration-level evidence: target check, transitive `#print axioms`, and
   a forbidden-token scan. A green module, `lean_ok`, `\leanok`, or report is not
   a substitute for this evidence; also check that the target is root-reachable.
4. Test for vacuity: zero/origin, empty or singleton domains, `Fin 0`, default
   values, impossible hypotheses, totalized operations, and predicates whose
   conclusion is already a field of the input.
5. Compare the source-facing obligation with the artifact. Label finite,
   model, conditional, proxy, and intrinsic results separately; an equivalent
   representation needs an explicit equality/iff/transport bridge.

Common shortcuts to flag are:

- changing the statement or weakening hypotheses until an easy proof compiles,
  then restoring them only in prose or an unrelated wrapper;
- adding a sequence of finite, local, or conditional certificates while the
  source-facing node and its first missing producer remain unchanged;
- moving a `sorry`/axiom behind a new structure, imported `.olean`, typeclass
  instance, or theorem alias;
- splitting one hard obligation into many easy declarations whose conjunction
  is never produced, or adding a target-shaped assumption as a field;
- proving an adjacent proxy and attaching it to the source node without a
  bridge, or marking a node `leanok` because a declaration merely compiles;
- claiming a clean build from the wrong target, stale artifact, or a source scan
  that did not inspect transitive axioms and consumers.

Useful trap families from the external formalization corpus include:

- binder and quantifier drift: an unused/impossible binder, an empty or
  singleton domain, `∀` replaced by `∃`, per-fibre evidence presented as a
  joint statement, or a one-sided endpoint substituted for a two-sided one;
- domain/model substitution: a finite, one-hot, coordinate, or toy model is
  attached to an intrinsic or asymptotic source claim without an explicit
  bridge and the hypotheses that make it valid;
- conclusion-shape drift: a `C∞`/regularity requirement, all-order constant,
  rate, positivity, or nonzero witness is silently weakened, moved into a
  premise, or made automatic by an over-strong assumption;
- evidence laundering: aliases, re-exports, `rfl` adapters, typeclass or
  `Nonempty` wrappers, imported `.olean` declarations, unrelated green leaf
  checks, stale graph caches, and generated metadata are counted as a new
  producer; and
- trust-boundary shortcuts: `sorry`/`admit`/`axiom`, project-specific axioms,
  or policy-disallowed native/code-generation escapes are hidden behind a
  friendly declaration. `#print axioms` and a source scan must settle this
  boundary, not naming or report prose.

These are prompts for a focused audit, not a blacklist: a finite model or
conditional interface can be valuable when it is labelled as such and a
proved bridge connects it to the source obligation. The external catalog's
T1–T15 names are useful vocabulary, but Horizon keeps the checks scoped and
evidence-based: [trap catalog](https://github.com/qinz1yang/auto-formalizing-skills/blob/main/prove/prove-TRAPS.md).

## Loop and convergence test

For repeated task rounds, write down the source-facing frontier, the first unmet
producer, and the expected irreversible state transition before reading the
latest report. Compare the previous two or three rounds by declaration/node
ids, not by commit-message adjectives. Count a round as substantive only when
it adds a source-relevant producer/consumer, discharges a tracked obligation,
or narrows a blocker with new evidence. Comments, generated timestamps,
aliases, wrappers, duplicate APIs, and repeated builds do not count alone.

Two consecutive rounds with the same first unmet producer and no decrease in
the source-facing empty/conditional frontier are a **churn signal**. A route
that weakens and then restates the same interface, or repeatedly renames the
same certificate, is a **loop** even if each commit compiles. Ask the strategy
reviewer for a route change or an explicit stop/escalation; do not reward more
local scaffolding. Preserve a real conditional result, but report it as
conditional and keep the headline incomplete.

## Report

Start with the shared review header from [[review-method]]. For each finding give
the exact declaration/node, classification, evidence, impact, and smallest
next action. Use `honesty`, `certificate`, `vacuity`, `packaging`, `evidence`,
or `loop` tags and state unchecked scope. File an inbox issue for an actionable
integrity defect and a memory for a reusable anti-evasion pattern. Do not edit
the artifact or mark a task done.
