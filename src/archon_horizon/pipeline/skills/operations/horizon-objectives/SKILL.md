---
name: horizon-objectives
description: Shape draft objectives into reviewed milestone architectures during preprocessing, then maintain evidence-based progress against the approved scope.
metadata:
  category: operations
---

# Edit objectives carefully

Read the current objective, revision, source links and affected graph evidence.
During milestone preprocessing the human text is a draft: workers and maintainers
may rewrite it as `## Step 1`, `## Step 2`, etc., each organizing a coherent chain
of milestones sharing an architecture, in dependency order. Preserve mathematical
intent and cited sources, not accidental wording or layout. Follow
[milestone contracts](../horizon-graph/references/milestones.md).

After approval, normally change only progress or one sentence whose meaning has
changed. A substantive route or contract correction requires strict review and
explicit baseline adoption. Keep the objective concise, not a session log.

Submit an exact-base document change through the roadmap repository and arrange
the required review; continue independent useful work while it is pending.
A completed session, passing build, or conditional theorem does
not establish a milestone. Mark a bullet complete only when its mathematical
scope and dependencies have the evidence the bullet claims. Statement acceptance
alone does not complete a proof checkbox. If no milestone
changed, leave the objective alone.

Keep executable decomposition in missions and dependencies in the graph. Keep
proof details, failed approaches, reports, and ownership in their durable
records; do not append them to a checklist. On a conflict, reread the latest
revision and make a bounded merge of the intended status change rather than
replacing the entire document.

Use `GET /api/v3/records/document/{id}` for the roadmap source location and
`GET /api/v3/repositories/{id}/file?commit_oid=...&path=...` for its actual
content. The document record does not contain editable inline Markdown. Use the
Forge change workflow to update source bytes, and read back the projected
source commit after merge. Link the resulting PR or accepted revision in the
assignment handoff.

To restore a damaged objective, inspect its Git history and recover the last
intended milestone structure. Retain justified later evidence/status updates.
Publish the repair as a new commit against the current head; do not rewrite
history or replace current source with an unexamined old snapshot.
