---
name: consumer-dependency
description: Trace whether a formalization result has substantive producer-to-consumer use, rather than only textual references, graph edges, or an attached name.
---

# Consumer and dependency trace

Use this lens when a task claims that a producer, bridge, helper, or headline
theorem is integrated. A declaration can compile and still be a dead island.

## Trace the route

- Start at the claimed producer and identify the intended terminal result or
  external consumer.
- Inspect actual Lean proof terms, imports, call sites, and representative
  downstream modules; separate substantive use from an index, a name search,
  or a discarded `have`.
- Compare the route with blueprint `\uses`, hgraph edges, task demand ledgers,
  and conditional/unconditional boundaries.
- Check whether a consumer reconstructs the result independently, invokes a
  weaker wrapper, or bypasses the advertised producer.
- Use a small deletion or replacement probe when it can distinguish a real
  dependency from a textual one. Do not require every helper to have a node.

## Report

Classify each route as `load-bearing`, `conditional scaffold`, `disconnected`,
or `unverified`. Give exact declarations/use sites, the probe or command, the
impact, and one next action. State unchecked dependents and confidence.

Do not judge theorem truth, source correspondence, API style, or full axiom
closure. Do not edit source or generated graph files. Pair with
[[load-bearing]], [[api-composition]], and [[graph-traceability]].
