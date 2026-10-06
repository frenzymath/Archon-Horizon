---
name: horizon-workspace
description: Develop in a prepared Horizon workspace and publish attributable Git checkpoints that other hosts can recover.
metadata:
  category: operations
---

# Publish shared source

The execution's working directory is its prepared checkout. Inspect
`git status` first and preserve unrelated changes. Other hosts see published
Git history, not this machine's uncommitted filesystem. Keep scratch clones and
large generated files under `$TMPDIR`; do not put credentials or provider logs
in the repository.

Commit selected source paths with ordinary Git when the sandbox permits Git
metadata writes. Inspect the diff and index before committing. The host detects
local work, journals its publication, and creates recovery commits for writable
source when Git metadata is protected. It preserves work on dedicated recovery
refs without changing the agent's branch or index. Do not assume recovery means
the destination default branch has been updated.

Inspect `GET /api/v3/assignments/{assignment_id}` for publication receipts.
`verified` means the stated commit reached the stated remote ref; `pending` or
`running` means delivery remains in progress; `failed` needs the retained error
examined. A transient network problem is retried by the host. Authentication,
missing local objects, and recovery-ref conflicts need repair by the configured
host operator. Do not run host administration commands with agent credentials.
Do not force-push, erase another worker's files, or report a local commit as
shared. Preserve the checkout while delivery is unsettled.

The host/control plane verifies publication receipts and commit identity. Use
those receipts rather than downloading and hashing every destination file as a
routine publication check. Inspect exact changed files or the relevant consumer
when validating mathematical content; investigate broader readback only when
there is concrete evidence of a publication mismatch.
For queued Forge changes, inspect the current
`GET /api/v3/operations/{idempotency_key}?operation=forge_change` result and its
`publication_receipt`, rather than repeatedly reading the initial POST response.
Use the verified branch/commit/artifact receipt with targeted diff and consumer
checks; transport verification does not establish mathematical correctness.

Before opening a Roadmap or library PR, verify the published commit and cite its exact
file and declaration. Workspace publication does not prove a graph node and
does not update accepted roadmap state. Use a Roadmap change through the Forge
workflow when the statement, labels, dependencies, or declaration locator must
change.

If Git metadata is protected by the sandbox, leave source edits in place and
report the pending files and commit state. The host's recovery publication can
preserve them; do not claim that a shell `git commit` succeeded when it did not.

Read [horizon-pipeline](../horizon-pipeline/SKILL.md) for publication receipt
reconciliation and [horizon-forge](../horizon-forge/SKILL.md) for protected
Roadmap changes.
