---
name: horizon-forge
description: Inspect repositories, create attributable changes and reviews, and manage Forge issues and PRs through Horizon's authenticated v3 API.
metadata:
  category: operations
---

# Work with Forge

Use the project's configured repository and integration records. Read exact
source at a pinned commit rather than trusting a moving default branch:

```text
GET /api/v3/repositories/$REPOSITORY_ID/head?branch=$BRANCH
GET /api/v3/repositories/$REPOSITORY_ID/file?commit_oid=$COMMIT&path=$PATH
GET /api/v3/forge-items/$ITEM_ID/inspect?view=files&expected_head_oid=$HEAD
```

Omit `branch` to resolve the configured default branch. Use the returned
`commit_oid` for file reads; `main` and `HEAD` are not exact commits. Inspection
views are `files`, `diff`, `comments`, `reviews`, and `review_comments`; the last
requires `review_id`. PR inspection requires the observed `expected_head_oid`.
List at most 100 items per page and follow the returned pagination cursor/page.

For a change, upload file contents as blob artifacts, then call
`POST /api/v3/forge/change` with the repository, base commit, and explicit file
operations. Inspect the completed operation and commit artifact before calling
`POST /api/v3/forge/create` for a PR. Never write directly to a protected
destination branch and never open a PR from an unverified local commit.
PR creation applies the phase label and the matching review policy's attention
labels (normally `awaiting-review`) before its delivery is complete. Check the
operation receipt; a label delivery retry must reuse that operation, not create
another PR. Maintainers inspect all open PRs in their repository, including
unlabelled ones, and decide which reviewers and workflow labels are appropriate.

Omit `base` when creating a PR to use the repository's configured default branch.
A different base requires `stack_reason` explaining the parent PR and path back
to the default branch. A staging branch is not the library's accepted destination.

Maintainers may fix an existing same-repository PR with `POST /api/v3/forge/change`:
include `forge_item_id`, use the inspected PR head as `base_commit_oid`, and supply
the usual file operations/blob hashes. Horizon amends that PR's existing branch;
it does not create another PR. Forks and the default branch are excluded from this
operation. Reinspect and review the resulting head before merge.

Use `POST /api/v3/forge/edit` to retarget or close a superseded PR, with
`forge_item_id`, `expected_head_oid`, `expected_base` (the current base branch),
and `base` (the new base) and/or `state` (`open` or `closed`). Explain supersession
in the PR discussion with the surviving PR/commit before closing. After a stack's
parent reaches the default branch, retarget its child there and inspect the new
diff. Both head and base changes invalidate earlier merge approval.

Use `POST /api/v3/forge/review` against the inspected head. Include the
reviewer descriptor and policy revision when available. Inline comments use a
repository-relative `path` and exactly one positive `new_position` or
`old_position`; the pinned head prevents commenting on stale code. Use
`POST /api/v3/forge/comment` for a general comment and `POST
/api/v3/forge/label` for state labels. Labels communicate workflow state; they
are not approval evidence.
General PR comments from a maintainer execution use the matching policy's
configured maintainer account. Specialist assessments use their prepared
invocation's `/report` endpoint and reviewer account. Do not add identity fields
to the general comment contract.

Maintainers alone merge through `POST /api/v3/forge/merge`, supplying the
current review gate, expected head, and authorized identity. A specialist
review is advisory. Re-read the PR head, checks, discussion, and graph impact
before merging. If the head changed, inspect again and review the new commit.

Use the dashboard or `GET /api/v3/forge-items?project_id=...` to locate current
items. For repository-only scope use
`GET /api/v3/records/forge_item?repository_id=...&status=open` so another
repository's backlog is excluded. Keep workspace issues for durable engineering or harness faults, graph
nodes for mathematical obligations, and Zulip for live decisions. Record the
published commit, files, checks, and remaining limitations in the handoff.
Read [change requests](references/changes.md) for exact file-change payloads and
operation reconciliation; use the review coordination skill for reviewer dispatch.

For objective runs, publication labels eligible PRs and issues `awaiting-review`.
This creates a durable item-specific maintenance request. After addressing changes,
use `request_review` with the new evidence/decision needed; the request coalesces
with an active owner. Maintainers use `settle_review` for their assigned generation.
A remote label alone is not evidence that a review round was accepted.
