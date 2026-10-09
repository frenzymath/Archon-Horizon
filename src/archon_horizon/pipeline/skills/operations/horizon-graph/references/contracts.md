# Graph source and evidence

## Locate the real source

Knowledge repositories contain `nodes/**/*.md` and `objectives/**/*.md`.
Explicit compatibility projects with `workflow: milestones` additionally contain buildable
`milestones/<Objective>/{Mi.lean,Definitions/,milestones.md}`; follow
[milestone contracts](milestones.md) for typed locators, route/contract gates,
human baseline approval and proof verification.
Read `GET /api/v3/records/node/{id}` or the document record to discover its
repository, path and commit; never assume the default project name or forge.
Follow those locators through the authenticated exact-commit file read.

The accepted source uses YAML frontmatter plus Markdown. Preserve existing
metadata and body structure. `label` (or `id`, otherwise the file stem) identifies
the node within the indexed repository; `title` names it; `labels` and `children`
are lists of strings. Referenced children must exist and edges must be acyclic.
In source documents, children are prerequisites. The API's `parent_node_ids`
field represents that prerequisite adjacency under the database naming: do not
invert source edges by guessing from the word "parent". Inspect source and
neighbors before altering a relation.

Change accepted source through the configured repository policy and PR workflow.
A catalog pointer alone is not approval of changed mathematics. After merge,
check the projected node/document source commit before claiming the dashboard
has incorporated the new content. A stale projection needs reconciliation;
opening a second PR with the same source does not repair indexing.

## What status evidence means

Use the repository's established labels and keep their meaning honest:

| Claim | Evidence |
| --- | --- |
| Informally stated | A precise standalone statement with necessary assumptions |
| Proof sketch | A concrete route that exposes its genuine gaps |
| Informally proved | An argument at the intended rigor, including boundary cases |
| Formally stated | The intended Lean declaration elaborates; admissions remain explicit |
| Formally proved | A proof of that declaration, with conditional prerequisites disclosed |
| Kernel checked | Exact environment, commit, file, declaration and successful command |

These are distinct statements about evidence, not a rigid mandatory sequence.
An admitted child can make a parent's route conditional even if the parent
declaration compiles. A parent's proof may already replace a listed child; then
repair the obsolete edge instead of concealing the mismatch. Inspect the trust
chain and source correspondence before claiming a milestone is complete.

## Phase and baseline

Pre-processing reviews the statement skeleton and decomposition carefully.
Main formalization usually needs proportionate roadmap consistency review, not
full library polishing of every workspace change. Post-processing reviews the
destination library according to its configured policies.

Default objective runs follow their versioned roadmap document. An explicit
compatibility formalization run pins `roadmap_snapshot_id`; compare that snapshot
with proposed contract changes. Correct a demonstrated source/statement error through
review, explain affected consumers and queue concrete repairs. Do not silently
reinterpret a frozen theorem to make proof work easier. Adoption of a revised
frozen baseline is an explicit maintainer run command, not an incidental node
edit. A new proof implementation alone does not require changing the statement.
