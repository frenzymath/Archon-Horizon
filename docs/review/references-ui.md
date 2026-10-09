# References dashboard implementation review

This review covers every named function in the new References implementation
and the existing functions touched to integrate it. Anonymous event handlers
were reviewed with their enclosing component. It is an implementation review
and a set of targeted tests, not a performance benchmark or a complete audit of
the existing dashboard.

## Function coverage

| File / function | Contract and review result |
| --- | --- |
| `referenceCatalog.ts` / `referenceDraft` | Converts the source revision and all editable fields into a browser draft; missing year remains blank rather than becoming zero. |
| `decodeReferenceDraft` | Validates saved field types, kind and positive integer revision, discards unknown fields, and preserves the original revision so refresh cannot silently rebase a draft. |
| `lines` | Preserves one author/URL per nonblank line; does not split commas inside names. |
| `referenceDraftError` | Checks required title, new citation-key syntax and integer years. Incomplete references remain valid; canonical identifier rules and deduplication remain authoritative on the server. |
| `referenceBody` | Omits citation identity and project ownership from edits; sends revision-checked `changes`; explicitly clears blank optional metadata. |
| `DesktopReferences.tsx` / `storedEditor` | Restores only a scoped, structurally valid editor identity from tab storage. |
| `Dialog` | Uses the browser's modal focus containment and Escape handling; unmount closes the native dialog. |
| `ReferenceMetadata` | Escapes plain metadata, activates only HTTP(S) URLs, and isolates new-tab links with `noopener noreferrer`. Does not fetch papers. |
| `BibTeX` | Lazily loads the authenticated text export using an account/project/revision-scoped cancellable query. Provides visible retry and download. |
| `ReferenceForm` | Keeps drafts separate from refreshed source data, compares the draft's source revision, requires an explicit reconciliation decision, respects write permission, and reuses the existing durable save helper for exact request recovery. |
| `DesktopReferences` | Bounds listing requests at 50 entries, debounces title/key search, paginates by opaque cursor, retains data on read errors, and verifies selected details belong to the displayed project before editing. |
| `api.ts` / `request`, `requestText`, `requestResponse`, `requestOnce` | JSON and text share cancellation, bounded read retries, timeouts, current authentication, and API error decoding. Mutations remain single-attempt requests. |
| `DesktopProjects.tsx` / `DesktopProjects`, `go` | Lazily mounts References with account/project identity; clears obsolete selection on navigation. Legacy milestone controls render only for persisted legacy milestone projects. |
| `PipelineApp.tsx` / `locationState`, `Shell`, `selectTab`, `projectView` | Adds project References navigation and shareable selection, clears selection on unrelated navigation, preserves old `project_view` links through normalization, and keeps connected/account permissions authoritative for browser editing. |
| `navigation.ts` / `projectDashboardUrl`, `canonicalDashboardUrl`, `dashboardUrl` | Uses the current `view` route parameter; retains a reference selection only for a References view. Existing document links previously emitted an ignored `project_view` parameter; this mismatch is corrected. |
| `Administration.tsx` / `ProjectCreate`, `ProjectEditor` | New projects explicitly select the graph workflow; existing idle projects may select it through the existing revision-safe catalog mutation. API lifecycle checks determine whether a workflow change is allowed. |

The new components reuse the existing `useSave` helper. Its request body and
idempotency key survive an unconfirmed acknowledgement; editing is locked during
that uncertainty. Browser coverage exercises recovery across a full page reload.
Reference lifecycle permissions, identifier normalization and uniqueness,
revision checks and server-side BibTeX serialization remain in the existing API.

## Validation

- TypeScript typecheck passed.
- Eight new unit/SSR/transport tests passed: stable citation identity, explicit
  metadata clearing, author boundaries, validation, corrupted draft handling,
  safe metadata rendering, reference links and authenticated text transport.
- The isolated Playwright References fixture passed search, cursor pagination,
  metadata/BibTeX display, safe links, concurrent revision detection, full-page
  draft recovery, exact lost-acknowledgement retry, write restrictions and
  retaining loaded data across a read failure.
- Existing navigation and transport checks were rerun after integration.

The browser fixture uses synthetic HTTP data and does not establish live API
authorization behavior; the existing backend tests cover those services. The
Reference UI intentionally edits metadata and exports BibTeX. It does not import
arbitrary BibTeX files, download papers, merge repository bibliography files, or
record a citation use on the user's behalf. Those remain explicit agent/API
operations documented in [Project references](../references.md).
