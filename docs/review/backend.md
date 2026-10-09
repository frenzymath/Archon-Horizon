# Backend source review

The 2026-10-08 review read the complete module contracts and every function body
in `pipeline/instructions`, `pipeline/review`, `pipeline/integrations`,
`pipeline/providers`, `search`, and `pipeline/_resources.py`: 35 modules and
284 functions, including methods, validators and nested helpers. The
[machine-readable catalog](backend-catalog.json) records each qualified function,
its source lines, disposition, linked findings and the reviewed file digest.
The [prompt implementation review](prompts.md) also covers project bootstrap,
which is outside this catalog's directory scope.

## Repairs from inspection

- Lean search now includes declarations with inline attributes and preserves
  their source locations and documentation. Cache generation 7 forces older
  incomplete indexes to rebuild; it is a serialization/extraction generation,
  separate from Horizon's release number.
- The MCP stdio server survives malformed JSON and non-object requests. It
  rejects invalid result limits before loading the index and publishes the same
  1–100 bounds in its tool schema. Unknown notifications receive no response.
- Native activity parsers guard the inspected malformed optional containers,
  counters and collaboration statuses. Codex user/developer/system messages
  remain excluded from agent narration. This preserves ingestion when those
  particular native schema fields are missing or malformed.
- Terminal review label cleanup converges: an already-correct outcome label is
  preserved, and stale review labels are removed without scheduling work for an
  already-settled projection.
- Remote `Retry-After` NaN/infinity values use normal backoff instead of reaching
  durable timestamp arithmetic. Ordinary numeric delay hints remain usable.
- Session and reviewer instructions are readable, packaged Markdown. New
  contexts pin their templates, phase contracts and complete reviewer packets;
  continuations use their retained bundle. Ledger inline budgets include row
  IDs, markup and JSON escaping, and objective continuations avoid duplicated
  startup/mission text.
- Reviewer check and job links use `/api/v3/lean/checks` and
  `/api/v3/lean/verifications`. Historical packet keys retain their old names
  for saved-manifest compatibility, without making graph milestones mandatory.

## Purpose and retained boundaries

| Reviewed area | Assessment |
| --- | --- |
| Instructions, resources and catalogs | Discovery metadata, current authoring files and immutable session bundles serve different lifetimes. Keep the restricted template loader and existing file/digest/byte bounds; replacing these with unrestricted formatting would weaken provenance. The dashboard's companion catalog change also prevents old resource content appearing beneath a newly selected entry. |
| Review contracts, accounts and guidance | Structured reports, native/durable reviewer modes and explicit account provisioning remain useful. Account secrets stay in private files, HTTP stays outside database transactions, and credentials are scoped to the live owning execution. Descriptors provide reusable rubrics rather than new permission roles. |
| Review invocation and packet handling | The larger preparation function coordinates policy/rubric/head pins, exact ownership, immutable evidence, capacity and idempotency. These branches represent distinct authorization and lifecycle checks; removing them because of line count would conceal meaningful failure modes. Behavioral prose is now authored separately in Markdown. |
| Review decisions, backlog, demand and labels | Review generations prevent delayed label delivery from erasing a new request. Advisory policies permit proportionate specialist selection; explicitly required legacy policies retain their configured reporting/coverage rules. Source-bound library compiler receipts support acceptance without choosing the mathematical plan. |
| Forge changes, inspection and operation receipts | Explicit source commits, operation markers, actor/project scope and payload bounds are purposeful. A publication receipt proves an observed external write, not agent completion or semantic mathematical correctness. |
| Connectors, communications and browser integration views | Network delivery is separated from transactional claims and fenced settlement. Uncertain writes reconcile their original operation rather than resend blindly. Topic identity, read receipts and review/head checks protect against stale observations. Public browser views omit private credentials. |
| Provider events and usage accounting | Normalized events isolate native formats; cumulative versus per-turn counters and parent versus child usage require separate handling to avoid double accounting or misleading model labels. Small defensive schema guards belong at these parser boundaries. |
| Search extraction, index and workspace discovery | This is an offline lexical index, with declaration/type/header search and source-root fingerprinting. It complements source inspection and external mathematical search; empty results do not establish absence of a theorem. Existing cache/build locking and explicit source roots remain useful. |

The review kept independently meaningful helpers and added comments where a
compatibility or trust boundary would otherwise look arbitrary. It did not split
every long function merely to meet a file-size target or replace explicit error
branches with broad catch-all handlers.

## Validation and limits

The latest focused instruction, reviewer, packet, label and native parser suite
passed **130 tests**. The preceding search/parser/objective/catalog suite passed
**121 tests**, including the new search and stdio regressions. The connector
suite passed **34 tests**, exercising the retry-hint repair and existing transport behavior.
Root integration checks cover the final shared checkout, distributions and
frontend; these scoped counts are not whole-repository coverage.

Unexpected nested remote response shapes can still escape
`ConnectorManager.dispatch_one` as Python shape errors. The claimed operation
keeps its outstanding lease; after expiry it is retried in reconciliation-only
mode, which prevents blind duplicate sends. The connector process can nevertheless
restart. This review documents that remaining schema-drift limitation rather than
claiming the adapter handles every possible external response.

Old bundles cannot reconstruct behavioral fragments that were never saved. Their
original core/function fields remain authoritative, with explicit installed
compatibility fallback for those missing historical fragments. No operator
database or saved policy was rewritten to adopt new instructions.

Reading a function is not evidence that every branch executed. The catalog
contains source-review dispositions, not a correctness certificate, performance
benchmark or live-provider test. Its digests identify this snapshot; subsequent
edits require fresh review of the changed bodies.
