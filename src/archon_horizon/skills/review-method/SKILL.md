---
name: review-method
description: A lightweight method for independent, evidence-based reviews of formalization work; choose focused lenses without turning review into a fixed pipeline or completion gate.
recommendation: >-
  Consider this skill when planning a review: state the scope and snapshot,
  choose only the lenses that fit, and report evidence and limits separately.
  It is advisory and is never a completion gate.
---

# Review method

Use this as a small protocol around one or more specialist lenses. A review is
useful when it answers a concrete question with evidence; it is not a second
proof session and it need not inspect the whole workspace.

## Frame the review

Before reading deeply, record:

- the project, files, declarations, blueprint nodes, or run being reviewed;
- the commit, source snapshot, and generated graph/check snapshot;
- the claim under review and explicit non-goals;
- the specialist lenses selected and the depth worth their cost.

Use a fresh context when independence matters. Read the prior report only after
forming the scope, so its vocabulary does not become an unexamined conclusion.

## Gather evidence

Keep these observations distinct:

1. **Source evidence:** what the cited page, TeX, blueprint, or requirement says.
2. **Declaration evidence:** the exact Lean type, body, imports, and consumers.
3. **Execution evidence:** LSP diagnostics, a target check, a build, artifacts,
   warnings, and axiom or `sorry` output.
4. **State evidence:** graph, task, roadmap, inbox, ledger, and run history.

A clean result in one layer does not prove a claim in another. Prefer a small
reproducible command or a precise file/line witness over a narrative summary.

## Judge and report

Separate confirmed defects, partial correspondence, unverified claims, and no
issue found. Order findings by consequence, not by discovery order. For each
finding give the location, the exact mismatch or risk, the evidence, and the
smallest useful next action. Say what was not checked.

Useful handoff labels are `blocker`, `fidelity`, `bridge`, `hypotheses`,
`evidence`, `attachment`, `api`, `graph`, `provenance`, and `health`. Use an
inbox issue for work another session must act on; use a node comment for a fact
belonging to one graph node. Keep reports concise and do not mark tasks done.

## Boundaries

Reviewers should not silently repair the artifact they are judging. A writer
may act on a finding in a later, scoped change. Preserve unrelated edits and
state why a tempting broader audit was skipped. See the specialist skills for
source fidelity, semantics, verification, API, graph, provenance, strategy, and
run health.

## Normalized report header

For mass-scanned reports, put the common fields first, even when a lane also
needs a domain-specific verdict:

```text
Status: satisfactory | partial | mismatch | unverified | needs-adjudication
Verdict: lane-specific result, if useful
Snapshot: exact revision and source/artifact snapshot
Scope: files, declarations, nodes, runs, and consumers examined
Inputs/checks: commands, signatures, source locations, or probes
Findings: id, severity (blocker|major|minor|note), location, claim, evidence, impact, next action
Unchecked: material limits of the pass
Non-goals: lanes intentionally left to another reviewer
Confidence: high | medium | low
```

Use `Status` for cross-lane scanning and `Verdict` for values such as
`converging`, `healthy`, or `reproducible`; do not make a lane-specific verdict
look like a universal completion claim.
