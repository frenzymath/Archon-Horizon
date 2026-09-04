---
name: graph-traceability
description: >-
  Audit hgraph and blueprint traceability: synchronization, labels, edges,
  closure, frontier, stale or dangling records, declaration consumers, and
  status honesty.
---

# Graph traceability

Use [[hgraph]] commands on one named project and scope the review to the
affected cone when possible. The graph is a useful map, not an independent
proof checker.

## Inspect

Run sync and read its complete output. Check:

- unresolved labels, `\uses`, `\lean` names, duplicate declarations, and
  stale/dangling nodes;
- edge direction and whether statement versus proof dependencies are recorded;
- node status, source boundaries, chapter membership, and actual consumers;
- closure/frontier counts against the intended theorem route;
- `lean_status` against the artifact evidence, rather than treating a source
  scan as a kernel check;
- generated timestamp/order churn versus substantive node or edge changes;
- roadmap/task references and status transitions for the same milestone.

Inspect the cone of a claimed terminal node, not only aggregate counts. A large
number of closed helpers can coexist with an empty headline node; an apparently
clean snapshot can hide an untracked or stale duplicate.

## Report and preserve

Include project, graph revision, command output summary, counts, and exact node
ids. Separate authored defects from generated churn and concurrent changes.
Never stage or revert a broad generated rewrite merely to make statistics look
clean. Use a node comment for a local mapping fact and an inbox issue for
cross-project or status coordination. Pair with [[blueprint-integrity]],
[[verification-evidence]], and [[provenance-isolation]].
