---
name: source-discovery
description: Map and verify external evidence for formalization work across local references, GitHub repositories and reviews, Mathlib source and PRs, Tau Ceti artifacts, and Zulip discussions without confusing discussion or search results with authoritative mathematics.
recommendation: >-
  consider this skill when a claim depends on material outside the checked-out
  project. Start locally, pin the exact source/revision or message locator,
  and record access limits; do not make external lookup a hidden prerequisite.
---

# Source discovery

Use this as a source map and evidence protocol. It helps the lead or a source
acquisition helper find the right material; it does not decide theorem truth or
replace `source-fidelity` and `external-boundary` review.

## Choose the channel

Search in this order when practical, widening only when the current evidence
cannot answer the question:

1. **Workspace sources.** Read `references/manifest.yaml`, the referenced TeX
   or page transcriptions, project blueprints, Lean modules, hgraph comments,
   and durable review artifacts. Pin the project commit and file path.
2. **The source's repository.** On GitHub, inspect the exact repository and ref,
   then the relevant file, commit, issue, pull request, review thread, or
   release. Issue and PR discussions explain intent and history; they do not by
   themselves prove a mathematical statement.
3. **Mathlib.** Inspect the checked-out `.lake/packages/mathlib/Mathlib/` source,
   module documentation, imported declarations, and the exact Mathlib version.
   Use upstream GitHub issues and PRs to understand API rationale or migration,
   but verify the final declaration and assumptions in the checked-out source.
4. **Tau Ceti material.** Search configured Tau Ceti repositories, review
   corpora, rubrics, PR diffs, and logs. Treat a rubric or review as an
   engineering or interpretation record; cite the underlying source or Lean
   declaration for a mathematical claim.
5. **Zulip.** Consult public streams, topics, and messages when maintainers'
   discussion is needed to resolve intent, convention, or an API transition.
   Record the stream/topic/message locator and timestamp. A discussion is
   context or expert evidence, not a substitute for a source or kernel result.

The order is a preference, not a gate. A task may use only local evidence, and
an inaccessible channel remains an explicit boundary rather than a reason to
guess.

## Access GitHub and Zulip carefully

- Prefer an official web/API endpoint or checked-out clone over search snippets.
  Follow links to the actual file, issue, PR, review comment, message, or
  commit, and capture its stable URL plus ref/SHA where available.
- For GitHub, distinguish repository contents at a pinned ref from mutable
  issue/PR conversation. Record whether a statement came from code, a maintainer
  comment, a review suggestion, or an unmerged proposal.
- For Zulip, use the API when a credential is already provided through the
  environment or approved secret mechanism. Use Playwright/MCP only when the
  user has authorized browser access and the API is unavailable or insufficient.
  Do not bypass access controls, scrape private material without permission, or
  claim to have read a message that was not accessible.
- Never paste API keys, cookies, authorization headers, or private message
  bodies into source files, reports, commits, prompts, or command output. Do
  not persist browser profiles or session artifacts unless the user explicitly
  requests it and the workspace policy permits it.
- Respect repository permissions, rate limits, terms, and the distinction
  between public, private, and deleted material. A failed or partial lookup is
  evidence about access, not evidence about the mathematics.

## Make the evidence reusable

For each material external observation, record:

- channel and locator (local path, GitHub URL plus repository/ref, or Zulip
  stream/topic/message URL and timestamp);
- exact revision, message timestamp, retrieval date, and access mode when they
  affect reproducibility;
- what the artifact establishes, what it merely suggests, and which hypotheses
  or trust boundaries remain;
- the next local verification step: read the source, inspect the declaration,
  reproduce the build, or ask for human adjudication.

Use concise summaries or short excerpts and keep the full source in the shared
`references/` library when licensing and workspace policy allow it. Do not copy
an entire discussion into a blueprint. `references/manifest.yaml` is the source
inventory; graph comments or review reports are the right place for scoped
interpretation and unresolved boundaries.

## Report the boundary

Classify an observation as `primary-source`, `checked-code`, `maintainer-context`,
`review-opinion`, `unverified`, or `inaccessible`. Separate “the source says,”
“a maintainer proposed,” “the current code implements,” and “the reviewer
infers.” If a source, message, or revision cannot be verified, state that in the
report and route it to `reference-retriever`, `transcription-fidelity-reviewer`,
`source-fidelity-reviewer`, or `external-boundary-reviewer` as appropriate.

Do not edit accepted source merely to record a discovery. The acquisition role
may update `references/` and its manifest; reviewers remain read-only and leave
issues or memories for the lead to reconcile.
