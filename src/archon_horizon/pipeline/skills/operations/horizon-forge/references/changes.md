# Exact-commit Forge changes

Discover repository IDs through `GET /api/v3/records/repository?project_id=...`;
the `purpose`, `remote_path`, integration, and `default_branch` describe each
repository. Do not assume a repository name or a universal branch policy.

1. Read `GET /api/v3/repositories/{id}/head` for the published base commit.
2. Read changed existing files with `GET /api/v3/repositories/{id}/file` using
   `commit_oid` and `path`. Retain each current blob SHA.
3. Upload the exact proposed source file with
   `horizon-pipeline agent upload-file PATH --project-id PROJECT_UUID`.
   It preserves the file bytes, defaults to `--media-type text/plain`, and journals
   `POST /api/v3/artifacts` with its idempotency key. The bounded file must fit the
   API request after base64 encoding (approximately 768 KiB). Use the returned
   artifact `id` in the change below. Do not construct base64 manually or use
   `json_content` for source text; that field serializes a JSON value.
4. Submit `POST /api/v3/forge/change`:

```json
{
  "repository_id": "<repository-id>",
  "origin_run_id": "<current-run-id>",
  "base_commit_oid": "<full-base-commit>",
  "message": "Align the compactness milestone with the source statement",
  "files": [{
    "operation": "update",
    "path": "nodes/compactness.md",
    "sha": "<existing-blob-sha>",
    "content_artifact_id": "<uploaded-artifact-id>"
  }]
}
```

`create` needs content and no old SHA; `delete` needs the old SHA and no content.
Paths must be normalized and repository-relative. A multi-file change contains
one operation per path. This creates a dedicated branch, not a default-branch
write. Follow the returned operation and commit artifact to obtain the actual
branch/commit; do not guess a branch name from the request.
Read `GET /api/v3/operations/{idempotency_key}?operation=forge_change` for the
current delivery state and `publication_receipt`. The initial POST response is
an enqueue acknowledgment. Use the verified receipt for commit/branch identity;
validate relevant changed content without downloading every unchanged file.

Create the PR with `POST /api/v3/forge/create`, including `repository_id`,
`origin_run_id`, the current `review_phase` (`preprocessing`, `formalization`,
or `postprocessing`), `kind: "pull_request"`, `title`, `body`, `head`, and
`base`. Issue creation uses `kind: "issue"` and omits head/base.

## Read reviews and recover uncertain delivery

Item inspection supports `files`, `diff`, `comments`, `reviews`, and
`review_comments`; the latter also takes the selected `review_id`. Page bounded
results with `page` and `limit`; retain `expected_head_oid` when inspecting PRs.
Read existing findings and author replies before adding another review.

The mutating API returns a queued operation. A receipt means Horizon accepted
the intent, not that Forge has applied it. Use `agent pending` for client-side
uncertain requests; inspect operation records and linked result artifacts for
external delivery. Preserve the same idempotency key after a lost response.
Do not open a duplicate PR, review, or issue to compensate for a timeout.

For a failed delivery, inspect its failure and fix the actual cause. Authorized
owners can use `retry_delivery` with the outbox operation's ID, current revision,
and explanatory note after repair. Review/merge retries require maintainer
authority. Do not cancel a required delivery solely to clear a completion gate.
