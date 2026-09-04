---
name: external-boundary
description: Audit imported, classical, conditional, and human-reviewed interfaces so a formalization does not present an assumed producer or external theorem as a proved headline result.
---

# External boundaries

Use this lens when a result depends on an imported theorem, a classical
interface, a conditional producer, or an expert judgment outside the checked
Lean cone. The aim is an honest contract, not a demand to formalize every
external result.

## Inspect

- Name the imported or assumed producer, its authoritative source and revision,
  exact hypotheses, conclusion, and trust boundary.
- Distinguish a theorem proved in the project from an interface, axiom,
  `sorry`-backed declaration, or conditional package supplied by another layer.
- Check that the producer's hypotheses are visible, that a direct consumer
  actually uses its output, and that a finite/model result is not labelled as
  the unconditional headline theorem.
- Record human-review, analytic, measure, gluing, attainment, or source
  adjudication obligations that the kernel cannot certify.

Do not retrieve or transcribe sources here; use [[references]] and the
`reference-retriever`/`page-transcriber` roles for acquisition. Pair with
[[source-fidelity]], [[consumer-dependency]], and [[verification-evidence]].

## Report

State the exact boundary, contract, consumers, evidence, unresolved obligation,
status (`proved`, `conditional`, `imported`, `unverified`, or `needs-adjudication`),
scope, and confidence. Do not silently strengthen the interface or mark a task
complete.
