---
name: horizon-graph
description: Inspect and maintain Horizon roadmap documents, nodes, dependencies, and evidence without confusing attempts with accepted graph state.
metadata:
  category: operations
---

# Keep the graph truthful

A mathematical node can have implementations in several target repositories. Progress and review
live in `implementations[repository_uuid]`, using the same states for workspace
and library. Select `target_repository_id` in graph/node/roadmap queries; use
`run_id` on the roadmap endpoint to select the run's destination. Never downgrade
workspace evidence to start a library port. Read
[repository progress](references/repository-progress.md) before editing these records.

In postprocessing, use a separate graph namespace for the integration strategy,
link source proofs and show accepted library coverage. Reuse existing implementation
evidence where it applies; new strategy nodes can express generalization or changed
interfaces without overwriting the source formalization graph. Keep cluster ownership across several PRs. Rewrite obsolete node
content freely in reviewed Git changes; Git retains history. Update dependencies
when changing routes, and use new linked nodes when splitting/generalizing results.

Read the project roadmap through `GET /api/v3/roadmap?project_id=...` and use
the dashboard graph endpoints for bounded node and dependency views. A node's
`source_repository_id`, `source_path`, and `source_commit_oid` identify the
contract that a proof must match. An open or rejected PR is an attempt, not
accepted graph state.

Inspect a node, its immediate prerequisites/consumers, declaration locator, labels,
and current revision before changing it. Each dependency must be a genuine
prerequisite whose statement helps imply the parent. Do not silently repair a
stale source pin by following a moving branch; propose a new reviewed change.

Keep statements and definitions aligned with the referenced source and Lean
declaration. A successful build does not justify a `formally_proved` label by
itself. Record exact commit, file, declaration, environment, and command for
kernel-checked evidence. Preserve conditional status when an admitted child
remains a real condition of the accepted route.

Roadmap edits are revision-aware. Read the current document and revision, make
the smallest coherent change, and submit it through the configured Forge change
workflow. On a revision conflict, reread and reconcile; never overwrite a newer
graph revision. Update dependent nodes and declaration references in the same
bounded change when the repair is obvious; otherwise record the affected owners
and open a focused follow-up.

Use [horizon-workspace](../horizon-workspace/SKILL.md) for source
commits and [horizon-objectives](../horizon-objectives/SKILL.md) for the user's
milestone document. Read [graph contracts](references/contracts.md) when changing
source documents, edges, status evidence, or a frozen baseline.

The default `workflow: graph` treats milestones as ordinary nodes with the
`milestone` label. Agents propose their names, decomposition and amendments through
roadmap PRs; no compiler receipt or frozen baseline is required merely for planning.
Ordinary source identity, dependency validity and review permissions still apply.

For explicitly retained projects with `workflow: milestones`, follow
[milestone contracts](references/milestones.md): route selection precedes Lean
contract PRs; helper ownership is distinct from logical dependencies; strict
contract review and human approval precede formalization.
