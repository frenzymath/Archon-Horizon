# Frontend source review

This review read complete bodies throughout `frontend/src/**/*.ts` and
`frontend/src/**/*.tsx`, plus both Vite configurations. Paths below are relative
to `src/archon_horizon/`. The [exact catalog](frontend-catalog.json) records 51
files, 1,130 functions with bodies and 417 top-level symbols, with source spans
and SHA-256 digests. Functions include anonymous callbacks, JSX handlers, nested
arrows, methods and accessors; declaration-only files have no executable bodies.
The symbol and function counts overlap and must not be added together.

The review considered contracts, failure handling, state lifetime, bounds,
explanatory comments and whether a function has a distinct responsibility. It is
source inspection with selected regression checks, not exhaustive execution or a
proof of correctness. A `reviewed` catalog entry does not mean every possible
input was tested. A changed digest requires rereading the changed source.

## Repairs

| Finding | Observed behavior and repair | Evidence |
| --- | --- | --- |
| `F-pagination-exhaustion` | Activity's historical-session cursor used the initial cursor again when the final page returned `null`. Distinguish an unstarted cursor (`undefined`) from exhaustion (`null`). Reset an aborted older-page loading state when refreshing. | Synthetic demo includes a historical page; browser verifies its final session appears and the load button disappears. |
| `F-kernel-status-colors` | `kernel_checked` was a selectable graph status but had no color entry, so DOT generation could fail. Add amber presentation consistent with a candidate; a kernel pass alone does not assert sorry-free completion. | Graph regression constructs this status and checks successful DOT generation. |
| `F-reviewer-current-category-draft` | Clicking the current Reviewers category reset its selection without asking about unsaved changes. Apply the existing discard guard and disable the action during a pending save. | Administration browser dismisses the confirmation and verifies the original draft survives. |
| `F-legacy-selected-view` | Earlier links with `project` and `node`, `objective`, `mission` or `reference` could open Overview rather than the relevant view. Normalize legacy query aliases and infer a view while respecting explicit selections. | Navigation unit cases and a bare node link loaded under the demo's repository subpath. |

Comments now identify Graphviz's 200-entry per-tab SVG cache as a heuristic,
the DOT label length/wrapping limits as presentation choices, and macro/YAML
budgets as resource bounds. These values are not mathematical constants or
measured performance optima.

## File and responsibility review

| File(s) | Contract and assessment |
| --- | --- |
| `frontend/src/main.tsx`, `frontend/src/pipeline/PipelineApp.tsx` | Application bootstrap, authenticated account selection, navigation and shared providers. URL normalization belongs in the shared navigation utility, avoiding competing legacy-link rules. |
| `frontend/src/pipeline/api.ts` | Typed browser requests, payload validation, account-scoped storage and transport errors. Durable intent IDs survive uncertain write outcomes. Browser state is an aid to reconciliation, not server authorization. |
| `frontend/src/pipeline/queries.tsx` | Query invalidation, scoped event streams, cancellable reads, optimistic revision checks and pending-intent reconciliation. Do not simplify uncertain writes into automatic retries with fresh IDs (`I-request-reconciliation`). |
| `frontend/src/pipeline/shared.tsx`, `frontend/src/pipeline/RunPhase.tsx` | Common fields, confirmations, notices, rich text, dates and phase labels. Shared presentation avoids divergent editor conventions; trivial handlers need no line-by-line comments. |
| `frontend/src/pipeline/DesktopProjects.tsx` | Project navigation and scoping of roadmap, graph, references and mission views. Tab switches preserve only the relevant query selections. |
| `frontend/src/pipeline/DesktopMissions.tsx` | Mission tree, details and revision-checked editing. Iterative tree assembly tolerates missing parents and cycles. A mission query currently chooses the Missions view without automatically opening its modal (`L-mission-selection`). |
| `frontend/src/pipeline/DesktopActivity.tsx`, `frontend/src/components/ActivityTab.tsx` | Run/session activity, streaming refresh, historical pages, transcript/ledger/document tabs and cancellation. Explicit read and pagination states prevent final-page replay. `ActivityTab` is large; separating its views is reasonable future work if state ownership remains clear. |
| `frontend/src/pipeline/DesktopAdministration.tsx` | Host/agent/account directory, settings dialogs and reviewer editing. Discard guards and pending-save state protect drafts across category/project changes and revision conflicts. |
| `frontend/src/pipeline/Administration.tsx` | Shared administration editors plus earlier settings/resource/connection shells. Some old shells coexist with the current desktop interface (`L-compatibility-views`); delete only after tracing imports and supported links, rather than by filename or age. |
| `frontend/src/pipeline/InstructionCatalog.tsx` | Installed skills, prompt templates and descriptors with Markdown/source views. Resource selection records its owning entry so filtering or following a relative link cannot retain another entry's resource under the wrong title (`I-catalog-resource-ownership`). |
| `frontend/src/pipeline/DesktopReferences.tsx`, `frontend/src/pipeline/referenceCatalog.ts` | Project reference directory, filtering, source details, BibTeX and metadata registration. See the [separate reference UI review](references-ui.md) for boundaries and browser evidence. |
| `frontend/src/pipeline/DesktopSearch.tsx` | Scoped search and result links. Query state and selected project/pool determine reads; results remain display data, not executable markup. |
| `frontend/src/pipeline/DesktopNative.tsx` | Integrated Forgejo/Zulip launch and availability presentation. Service permissions remain the integration/server's responsibility. |
| `frontend/src/pipeline/Milestones.tsx` | Legacy milestone contract views retained for explicitly legacy runs. Objective runs use ordinary graph nodes and the milestone display label. |
| `frontend/src/components/FormalizationDAG.tsx`, `frontend/src/formalizationGraph.ts` | Graph preparation, status/filter rules, DOT escaping, selection, focus and math overlays. Only the selected node receives the rich math overlay; graph-wide labels remain compact. Status colors must cover every selectable status. |
| `frontend/src/vizInstance.ts`, `frontend/src/vizWorker.ts` | Lazy Graphviz worker, serialized requests and cached SVG responses. The 200-entry bound limits retained result count, not total bytes. Generated SVG is produced from escaped graph inputs; it is not an arbitrary uploaded SVG viewer. |
| `frontend/src/components/DocumentView.tsx`, `frontend/src/components/ActivityDocumentReference.tsx` | Source/blueprint document views and activity-linked document selection. Node-detail responses include the selected row in `nodes`, matching the document reader's contract. |
| `frontend/src/components/BlueprintMarkdown.tsx`, `frontend/src/components/RoadmapMarkdown.tsx`, `frontend/src/components/MarkdownPre.tsx` | Markdown, citations, math, source maps and fenced source. Source lines and node mappings are presentation aids; text is not proof evidence. Macro input size and YAML alias bounds avoid unbounded parser work. |
| `frontend/src/components/Bibliography.tsx`, `frontend/src/utils/bibliography.ts` | Citation parsing and bibliography rendering. A bibliography entry's existence does not verify its scholarly metadata or mathematical claims. |
| `frontend/src/components/MathTitle.tsx`, `frontend/src/components/MathTitleFormula.tsx`, `frontend/src/utils/mathTitle.ts` | Concise graph/document titles and isolated formula rendering. KaTeX errors remain presentation failures rather than blocking the underlying graph record. |
| `frontend/src/components/RecordDates.tsx`, `frontend/src/components/TagList.tsx`, `frontend/src/components/SessionActivityTags.tsx` | Record timestamps, tag editing and session labels. Stored values are distinguished from formatted labels and pending editor state. |
| `frontend/src/utils/activityPresentation.ts`, `frontend/src/utils/horizonReferences.ts` | Human-readable activity summaries and structured Horizon document references. Payload shape checks prevent assumptions about missing or older event fields. |
| `frontend/src/utils/navigation.ts`, `frontend/src/utils/capabilityLinks.ts`, `frontend/src/utils/sourceLinks.ts` | Canonical scoped URLs, instruction links and source-line navigation. Legacy URL compatibility is normalized once; tab-owned parameters prevent selections leaking across views. |
| `frontend/src/utils/missionTree.ts` | Cycle-tolerant iterative mission layout. A missing parent is displayed without inventing ownership. |
| `frontend/src/utils/document.ts`, `frontend/src/utils/blueprintSyntax.ts` | Limited blueprint parsing and Lean document extraction. Bounded input conventions and explicit malformed-input behavior are more useful than introducing a general parser service for each helper. |
| `frontend/src/utils/leanHighlight.ts`, `frontend/src/utils/sorryScanner.ts` | Lightweight lexical display of Lean and `sorry` occurrences (`L-lexical-lean-display`). Unicode identifiers, multiline lexical state and quoted constructs can exceed this approximation; trusted Lean checks live in the worker verification path. |
| `frontend/src/showcase/main.tsx`, `frontend/src/showcase/transport.ts` | Reuse the real dashboard with closed synthetic transport and demo-local navigation. See the [demo/tooling review](docs-demo.md); no production fetch fallback is installed. |
| `frontend/src/vite-env.d.ts`, `frontend/src/types/citation-js.d.ts` | Declaration-only integration contracts. No function bodies to execute or review as runtime code. |
| `frontend/vite.config.ts`, `frontend/vite.showcase.config.ts` | Separate production and standalone-demo build outputs. Relative demo assets support repository Pages paths; a demo build cannot overwrite the wheel's dashboard output. |

Small helpers generally express separate formatting or boundary checks; merging
them solely to reduce file/function counts would obscure those contracts. The
large editor/activity components merit future decomposition around independent
views, but a mechanical extraction without state ownership would add prop and
callback indirection. Branch counts alone do not establish either a defect or a
reason to refactor.

## Validation and limits

Navigation, graph/status/layout, TypeScript checking, desktop-administration
browser checks, the demo production build, synthetic transport checks and demo
Playwright navigation pass after these repairs. The demo browser verifies
objective/graph/reference/activity views, Markdown ledger rendering, selected
node reload, pagination exhaustion and read-only controls below
`/Archon-Horizon/demo/`, with no API or external network requests.

This frontend review does not test database transactions, provider recovery or
mathematical validity. Extremely deep imported session ancestry still uses a
recursive display path; no documented production-depth bound was established by
this review. The remaining UX/compatibility/lexical observations above are
explicit limitations, not claims of proven runtime failures.
