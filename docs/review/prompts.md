# Instruction and reviewer implementation review

The review covers every Python function in the seven modules listed below,
including nested helpers and model validators. It examines their current
contracts, branches, bounds, side effects and existing tests. It is a scoped
implementation review, not a claim that all Horizon functions were audited.
The [extended backend review](backend.md) records complete function coverage for
the surrounding instruction, review, integration, provider and search modules.

## Changes and invariants

Behavioral instructions are readable Markdown under
`src/archon_horizon/pipeline/instructions/templates/`. The installed instruction
catalog exposes every template with its content digest. Its dashboard browser
lists current templates and legacy compatibility templates separately. The
reserved `prompts/` bundle namespace cannot be overridden by added project
skills.

New contexts pin all templates in their existing skill artifact. Retained phase,
completion, recovery and prepared-review prompts read that artifact rather than
the current installed source. Prepared reviewer manifests also retain the fully
rendered prompt and complete immutable review context. Named `{{identifier}}`
fields interpolate once; JSON braces and shell variables are literal text. A
missing or corrupted new-bundle template fails explicitly instead of changing
instructions silently.

Older bundles contain core, planner and maintainer text but no prompt template
index. Their original fields remain authoritative. Fragments that were never
pinned historically use the packaged compatibility text; the old missing text
cannot be reconstructed from those bundles. A new context is needed to adopt
new behavior deliberately. The legacy supervisor template remains visible only
under `legacy/orchestrator`; preserving it supports already-persisted runs.

Objective continuations carry current mission scope and commitments without
repeating planner/maintainer startup. Duplicate mission text is replaced by a
ledger reference. Both Markdown and JSON ledger rendering charge identifiers,
markup and (for JSON) character escaping against the aggregate inline budget.
Exact omitted content stays in linked records or immutable snapshots. A bounded
briefing therefore never proves all obligations were inspected or fulfilled.

## Function coverage

| Module | Functions inspected | Findings and disposition |
| --- | --- | --- |
| `instructions/prompts.py` | `catalog`, `_phase_repositories`, `_review_readiness_batch`, `operations_context`, nested `size` and `sanitized`, `orchestrator_context`, `_orchestrator_goal`, `_pinned_bundle`, `objective_goal`, `goal`, nested `excerpt` | Fixed live-source phase/handoff/recovery fragments, objective startup repetition, duplicated seed text and aggregate ledger bounds. Objective repository lookup now permits a document without a frozen snapshot. Operational snapshots remain read-only, project-scoped and bounded; their textual annotations describe data, not new authority. |
| `instructions/bundles.py` | `_skill_metadata`, `skill_overview`, `skill_files`, `skill_path` | Extended the existing deterministic byte/file bounds to packaged templates; metadata remains discovery-only. Symlinks are rejected and installed templates win over added skill sources. Duplicate skill/subagent names remain rejected. |
| `instructions/instruction_catalog.py` | `read_catalog` | Added lazy template discovery and preview. Existing file membership, UTF-8 and preview-size checks apply; previews do not edit project descriptors or retained bundles. |
| `instructions/templates.py` | `template` | Added restricted, single-pass field substitution and digest-checked pinned resolution. Legacy fallback is explicit. Name validation prevents traversal; unresolved or extra fields fail. |
| `projects/bootstrap.py` | `RecipeRecord.known_kind`, `launch`, `expanded`, `resolve`, `preview`, `operator`, `apply` | Extracted seeded policy prose. Existing explicit recipe ordering, reviewed IDs, credential references, transactional application and idempotency checks remain necessary. Default advisory guidance treats graph milestones as planning data; destination libraries must be sorry-free. Existing saved policy records are not overwritten. |
| `review/invocations.py` | `ReviewerPrepare.explicit_retry`, `report`, `_maintainer`, `_owned`, `provider_limits`, `_capacity`, `prepare`, `prepare_assignment`, `_assignment_result`, `_work_key`, `_verification_evidence`, `_verification_discovery`, `_existing_work`, `_record_work`, `_prepare`, `assignment_manifest`, `assignment_request_fields`, `read`, `attach`, `bind_native`, `_bind_observed_child`, `cancel`, `release_child_claims`, `account_observed_child` | Extracted reviewer startup, reporting, optional plan/retry instructions and durable lifecycle into parent-pinned templates. Preparation remains authorization rather than launch. Exact-head/rubric/policy ownership, diagnosed retries, physical-stop confirmation and capacity settlement remain enforced Python. Verification discovery retains legacy packet field names for compatibility but uses the general Lean API; they supply evidence rather than prescribe mathematical strategy. |
| `review/packets.py` | `encode`, `prepare_packet` | Moved substantial reporting requirements to pinned templates. Structured report fields, exact-source links, byte budgets and complete-artifact locators remain Python data. Prior objections must remain available in the complete context even when omitted inline. |

## Validation

Focused regressions cover retained and historical catalogs, fail-closed template
resolution, literal interpolation, reviewer template/packet provenance, and both
ledger budgets. Existing prompt, review invocation, review packet, reviewer
preset and instruction catalog tests check behavior across these boundaries.
Distribution validators now require every source template in both wheel and
source archives, including reviewer startup instructions.
