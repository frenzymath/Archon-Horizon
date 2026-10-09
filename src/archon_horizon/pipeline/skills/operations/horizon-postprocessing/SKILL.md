---
name: horizon-postprocessing
description: Turn a completed Horizon workspace formalization into reviewed, fully proved destination-library contributions.
metadata:
  category: operations
---

# Postprocessing

Begin from the pinned completed workspace source. Revisit literature where it
helps improve mathematical meaning and library design. Propose concise integration
objectives and reviewed milestones; generalization or a new representation may be
useful. Keep the strategy in the roadmap and obtain a maintainer decision.

Use a separate graph namespace, for example node IDs `postprocessing/M3`, and links to original nodes. The `milestone` label controls display;
additional namespace/provenance metadata is a project convention. Put workspace files under `Postprocessing/`
and Lean namespaces such as `Postprocessing.M3` on the visible workspace main
branch. Use safe per-session worktrees for editing and publication. A prime in a
display name is optional; stable identity must not depend on punctuation.

Reuse sound source proofs when applicable. Roadmap skeletons may carry admitted
milestones; destination-library PRs require complete proofs and checked axiom
closure. Link each PR to its roadmap target and explain the contribution's global
purpose. Verify the destination checkout using `POST /api/v3/lean/verifications` with
`workspace_id`, `source_commit_oid` and `base_commit_oid`. Follow the job at
`GET /api/v3/lean/verifications/{id}`, then its `check_id` at
`GET /api/v3/lean/checks/{check_id}`. Jobs are host-owned and receipts apply only
to the exact head/base commits. The host derives the changed
library modules and rejects admissions; an import/layout failure needs repair.

Maintainers assess public interfaces, meaning, checks and unresolved findings,
selecting specialists when useful. Workspace completion and accepted library work
are different facts. Record integration evidence in the separate graph and request
phase acceptance when the requested library scope is integrated.
