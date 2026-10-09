---
name: horizon-preprocessing
description: Develop the literature, objective and reviewed milestone contracts for an objective-led Horizon project.
metadata:
  category: operations
---

# Preprocessing

Start with the human's draft objective and respect its instructions. Use literature
review to establish precise sources, stable identifiers and BibTeX references.
Propose concise objective bullets and milestones that cover the project and expose
useful intermediate results. Granularity and route are mathematical decisions.

Propose roadmap changes through PRs. Lean theorem skeletons may contain `sorry`;
supporting definitions and statement types must remain concrete. Preserve source
locations needed to judge faithfulness. Maintainers choose useful review perspectives
and accept the roadmap strategy. Milestones are ordinary graph nodes carrying the
`milestone` label; their names, number and decomposition are agent decisions. Keep
accepted meanings stable; propose justified corrections or additions through reviewed
PRs and account for affected dependents.

This is intended guidance, not a required sequence of agent sessions. A blueprint
objective can stop at the requested outcome. The maintainer records `accept_phase`
with evidence after accepting the strategy. Requested phases advance automatically
unless the run is configured for human approval. The default graph workflow has no mandatory milestone verifier or frozen-baseline
packet. Explicitly retained `workflow: milestones` projects use the compatibility
procedure linked from horizon-graph.
