---
name: formalization-review
description: Advisory router for selecting focused formalization review skills and subagents by claim risk; keeps source meaning, Lean evidence, blueprint metadata, API composition, and workspace operations as separate questions.
recommendation: >-
  consider only the focused review lanes that fit the changed claim. This is a
  routing aid and shared vocabulary, not a mandatory sequence; it is not a
  completion gate.
---

# Formalization review

Use this file to choose a small, independent review rather than applying one
large checklist. The main Horizon agent remains responsible for deciding the
order, depth, and stopping point. A reviewer may report `no-issue`; these skills
are not defect quotas.

## Select a lane

| Question | Skill / subagent |
| --- | --- |
| Did the task make honest, convergent progress? | `progress-integrity` / `work-reviewer` |
| Does the source claim have the same mathematical shape in Lean? | `source-fidelity` / `source-fidelity-reviewer` |
| Is the predicate or theorem mathematically meaningful and non-vacuous? | `semantic-adversarial` / `mathematical-correctness-reviewer` |
| Is the proof's advertised route and hypotheses load-bearing? | `load-bearing` / `proof-load-bearing-reviewer` |
| Is the blueprint attachment and graph metadata honest? | `blueprint-integrity` / `blueprint-integrity-reviewer` |
| Is this the canonical API and are alternate representations bridged? | `api-composition` / `api-composition-reviewer` |
| Does the declaration have substantive consumers? | `consumer-dependency` / `consumer-dependency-reviewer` |
| Is the implementation maintainable and compatible downstream? | `lean-quality` / `lean-quality-reviewer` |
| Does the reported build/check actually cover the claim? | `verification-evidence` / `verification-integrity-reviewer` |
| Are graph, roadmap, and status records internally consistent? | `graph-traceability` / `graph-traceability-reviewer` |
| Do files and claims belong to the right project and revision? | `provenance-isolation` / `provenance-integration-reviewer` |
| Is the proposed architecture/route still viable at project scale? | `strategy-convergence` / `strategy-reviewer` (with `api-composition-reviewer` for local API boundaries) |
| Did the run itself terminate and integrate cleanly? | `run-health` / `run-health-reviewer` |
| Is a certificate or completion claim honest across Lean, source, and process layers? | `honesty` / `honesty-reviewer` |
| Where is the existing Mathlib/workspace abstraction or documentation? | `mathlib-orientation` / `api-composition-reviewer` |
| Where can authoritative source or maintainer context be found? | `source-discovery` / `references` / `reference-retriever` |
| Are imported or conditional producers and human boundaries explicit? | `external-boundary` / `external-boundary-reviewer` |
| Was a PDF source transcribed faithfully? | `transcription-fidelity` / `transcription-fidelity-reviewer` |
| Is a public or milestone package reproducible? | `release-reproducibility` / `release-reproducibility-reviewer` |
| Does a disputed high-severity finding reproduce? | `review-adjudication` / `review-adjudicator` |

The names on the right are dispatchable helpers; the names on the left are
skills that writers and reviewers can load. Existing operational helpers remain
distinct: `ground` handles broad workspace strategy, `janitor` handles control
plane hygiene, `debug` and `lean-isolator` diagnose infrastructure, and
`reference-retriever`/`page-transcriber` acquire source material.

## Across the lifecycle

- During source intake, read the passage and its conventions before drafting an
  anchor or choosing a Lean object.
- During implementation, select only the semantic, proof, API, or graph lane
  that can answer the current risk; keep diagnostic probes out of accepted
  source.
- During handoff, separate mathematical, kernel, graph/render, and release
  evidence and record any unresolved boundary.

## Shared principles

- Treat task reports, prior reviews, and status labels as claims to verify, not
  as evidence by themselves. Start from the exact revision and source snapshot.
- Keep mathematical, Lean/kernel, blueprint/graph, and release statuses
  separate. A green build, a reference count, or `\leanok` marker does not
  establish the other dimensions.
- For Lean evidence, consult the focused verification lane and its
  declaration-level `#print axioms` checks rather than treating a green root
  build as closure.
- For a new certificate, context package, or completion claim, classify it as a
  proved producer, conditional interface, imported boundary, axiom/sorry-backed,
  empty/vacuous, or unverified. An empty structure is not an axiom, but it
  carries no evidence; a target-shaped premise returned as the proof is
  hypothesis packaging. Use [[honesty]] to keep these boundaries explicit.
- For repeated rounds, compare the source-facing frontier and first unmet
  producer by declaration/node id. Two rounds with no state transition in that
  frontier are a churn signal even when commits compile; use [[honesty]] and
  [[strategy-convergence]] before investing in another wrapper or re-expression.
- Prefer the smallest scoped probe that can settle a question. Widen to
  consumers, imports, or the dependency cone only when the first evidence calls
  for it.
- Preserve uncertainty: use `satisfactory`, `partial`, `mismatch`,
  `unverified`, or `needs adjudication` instead of forcing pass/fail.
- Reviewers are read-only on source and do not mark tasks done. They may write
  a concise report and an inbox issue or memory for a durable finding.

## Source contract

For source-backed work, identify the exact passage, definition, convention, and
standing assumptions before judging a declaration. Compare objects, domains,
quantifiers, endpoint conventions, regularity, hypotheses, and conclusion
polarity. Keep a source-facing object distinct from a coordinate/model proxy.
An equivalent formulation is a useful implementation only when the relationship
is proved and its hypotheses are visible; it is not a license to replace the
anchored statement.

## Representations and bridges

Coordinate formulas, local models, and computational proxies may be excellent
implementation tools, but they are not automatically the source definition.
Keep a source-facing declaration and a proxy separately named; connect them with
an equality, equivalence, or implication theorem whose hypotheses are visible.
Search for an existing canonical abstraction before introducing a duplicate.

## Claim evidence

For every material assertion, record a reproducible witness: a declaration
signature, source location, use site, build target, diagnostic, axiom output,
counterexample, or deletion probe. State what was not checked. For a proposed
repair, the compact `Fix / Context / Preserve` shape helps avoid changing a
verified cone merely to make a report look clean.

## Mass-scan report shape

Focused reviewers should make reports comparable without turning them into a
rigid workflow:

```text
Status: satisfactory | partial | mismatch | unverified | needs-adjudication
Verdict: lane-specific result, if useful
Snapshot: revision and source snapshot
Scope: files, declarations, graph nodes, and consumers examined
Inputs/checks: commands, signatures, use sites, or probes
Findings: id, severity (blocker|major|minor|note), location, claim, evidence, impact, next action
Unchecked: material limits of the pass
Non-goals: lanes intentionally left to another reviewer
Confidence: high | medium | low
```

Load the focused skill named by the selected lane for its domain-specific
questions. Do not duplicate those questions in a general progress review.

This menu is not a completion gate: the Horizon agent may skip a lane when its
evidence would not improve the decision.
