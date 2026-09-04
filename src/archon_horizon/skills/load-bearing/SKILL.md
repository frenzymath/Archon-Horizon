---
name: load-bearing
description: Check whether declarations, hypotheses, fields, and dependencies genuinely influence a formalization result, or merely create the appearance of progress around an unused scaffold.
---

# Load-bearing checks

Use this lens for large data structures, target-shaped assumptions, milestone
claims, and files with many helpers. A declaration is load-bearing when its
statement or proof materially supports the claimed consumer, not merely when it
exists or appears in a report.

## Trace influence

Starting from the claimed theorem or blueprint node:

- follow imports, `\uses`, graph edges, and actual declaration references;
- identify which fields and hypotheses occur in the conclusion and proof;
- inspect whether a producer constructs the datum rather than assuming the
  headline property as an `Is*`/`Has*` field;
- search for real consumers, including downstream projects and source-facing
  nodes;
- perform a safe deletion or replacement probe when it can show that a field,
  assumption, or helper is irrelevant;
- distinguish a useful conditional interface from a theorem that merely
  projects its target-shaped assumptions.

Do not demand that every helper have a blueprint node. Do demand that a
mathematically significant claim have a visible producer and consumer, or an
explicit reason it is intentionally staged.

## Report

For each suspected phantom dependency, name the declaration/field, the claimed
role, the observed use site, and the smallest separating test. Classify it as
load-bearing, conditional scaffold, disconnected, or unverified. Do not delete
or rewrite source while reviewing; hand a confirmed issue to the owner. Pair
with [[api-composition]], [[semantic-adversarial]], and
[[graph-traceability]].
