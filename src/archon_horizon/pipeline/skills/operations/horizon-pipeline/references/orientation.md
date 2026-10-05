# Horizon Records

Use this reference when a record's meaning or authority is unclear. It is not a
startup checklist.

| Record | Meaning |
| --- | --- |
| Mission | A scoped outcome, acceptance criteria and ownership subtree |
| Run | A phase on a mission, repository inputs and execution limits |
| Assignment | One owner's task, retained context and unfinished commitments |
| Execution | A host's leased attempt; lease loss ends its authority |
| Obligation | A result to deliver or give an identified durable owner |
| Native child | A local helper whose parent collects and integrates the result |
| Graph node | A mathematical statement, dependency or source-bound progress claim |

A mission tree organizes responsibility. The mathematical graph describes
dependencies, including dependencies across mission branches. Neither shape
guarantees that statements compose: interfaces and integration still need review.
Scope checks, revisions and leases prevent stale or unauthorized mutations.

| Question | Source of truth |
| --- | --- |
| Who owns work and may execute? | Missions, assignments, conditions and leases in the API |
| What is the accepted roadmap? | Reviewed source revision; graph views project that revision |
| What code was shared? | Published Git commit and verified publication receipt |
| What was checked? | Exact-input check receipt and its recorded scope |
| What was reviewed? | Pinned head, descriptor revision, findings and acceptance |
| What was communicated? | Current Forge/Zulip records and confirmed delivery |
| What instructions apply? | Pinned catalog, assignment and destination policy |

Agents use the API, not database writes. Native names and reviewer identities do
not grant additional authority. A child maintainer can close its subtree, not
its ancestors. The root maintainer owns phase completion.

The host handles leases, context continuation, checkpoints and publication
recovery. An execution ending is not mathematical acceptance, and a harness goal
is not another authority. Read the [entrypoint](../SKILL.md) for role contracts or
[phase outcomes](phases.md) for acceptance boundaries.
