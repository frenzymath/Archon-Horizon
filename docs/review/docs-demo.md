# Documentation, demo and review-tool audit

This review covers the newly added documentation/demo tooling and its functions.
It does not establish that the rest of Horizon has been reviewed function by
function. The code-derived pipeline specification remains in
[implementation audit](../design/implementation-audit.md).

## Python functions

| File | Functions read | Findings and boundaries |
| --- | --- | --- |
| `scripts/generate_dashboard_fixture.py` | `fixture`, `main` | Literal synthetic records only; no snapshot, environment-secret, database or network input. Stable timestamp and JSON formatting make `--check` reproducible. CLI writes only its selected output. Proof labels and bibliography are explicitly invented. |
| `scripts/docs_hooks.py` | `on_files`, `on_page_markdown`, `on_page_markdown.replace` | Root guides are virtual MkDocs pages, avoiding duplicated source. Relative guide links become site links; code/config links point to the selected build ref. Absolute URLs and fragments retain their meaning. Link rewriting is for ordinary inline Markdown; arbitrary HTML or complex reference-style links are outside its contract. |
| `scripts/assemble_docs_demo.py` | `main` | Requires both completed artifacts, copies only the standalone build, and refuses a symlinked demo destination. The selected site's generated `demo/` subtree is replaceable; source and normal wheel dashboard output are not touched. |
| `scripts/review_inventory.py` | `metrics`, `metrics.visit`, `functions`, `functions.descend`, `scan`, `markdown_report`, `main` | Parses without imports, inventories nested scopes, excludes generated trees, retains syntax failures and distinguishes unknown coverage from zero. Branch/nesting counts exclude nested callable bodies; function-span comment/coverage counts include nested definitions and are documented as span evidence. An explicit ledger marks review evidence; metrics never infer it. |
| `scripts/version.py` | `_source_version`, `_json_versions`, `_read_versions`, `check`, `_replace_once`, `set_version`, `main` | Prerelease badge parsing now handles hyphens and Shields escaping. All markers and JSON inputs are validated before writing; missing markers or malformed JSON leave every file unchanged. This prevents validation-time partial synchronization, not an all-files atomic filesystem transaction. |
| `scripts/check_wheel.py` | `main` and all required/forbidden predicate lambdas | Checks exact current Python modules, Markdown templates, expected data and byte equality, and rejects retired/local/toolchain trees. This checks distribution contents, not installed functionality. |
| `scripts/check_sdist.py` | `check_archive`, `main` | Validates paths/types, compares included source bytes with the checkout and requires Python modules, templates and selected rebuild assets. Does not extract an untrusted archive. |

The [tooling catalog](tooling-catalog.json) binds this review to 7 complete
scripts, 53 function bodies (including predicate lambdas) and 36 top-level
symbols. Hashes and source spans describe exact review scope. The
[frontend review](frontend.md) and its [catalog](frontend-catalog.json) cover the
dashboard runtime separately.

The inventory's own `scan` function has several branches because parsing,
coverage and review evidence are optional. Its responsibilities share one file
read/parse pass; splitting them into application services would add dependencies
without an independent runtime contract. The generator's larger `fixture`
function primarily declares the example data. Its size is not evidence of an
orchestration policy or a reason to generalize it into a real-data exporter.

## Dashboard and browser functions

| File | Functions read | Findings and boundaries |
| --- | --- | --- |
| `frontend/src/showcase/transport.ts` | `demoResponse`, `page`, response `json` helper | Closed allowlist of synthetic reads. Unknown reads return an explicit error; every non-GET returns 403. Search operates on the example catalog/directory. No fallback to production fetch exists. |
| `frontend/src/showcase/transport.ts` | `installDemoTransport`, replacement `fetch`, `DemoEvents.constructor`, `DemoEvents.close` | Installed before the real dashboard mounts. Abort is respected before response creation. Event-source replacement settles connection state entirely in memory; close prevents a late open. It does not simulate server events or execution recovery. |
| `frontend/src/showcase/transport.ts` | `installDemoNavigation`, history adapters and click handler | Rewrites production `/pipeline` history to the demo's current pathname, retaining query selection and reload. External and API navigation is blocked. It is demo-only and does not change the authenticated application's routing. |
| `frontend/src/showcase/main.tsx` | startup and lazy dashboard import | Uses the actual application with a visible synthetic/read-only banner. No account login or service endpoint is configured. |
| `frontend/tests/showcaseTransport.test.ts` | `main` | Checks viewer permissions, filtering, mutation/unknown-route failures and synthetic BibTeX. |
| `frontend/tests/showcase.browser.cjs` | static server handler, browser scenario and cleanup | Serves only the build below a repository subpath. Checks real navigation, Markdown objective/ledger rendering, graph generation, reload, references and read-only controls. Captures browser errors and rejects API/external network requests; browser and server close in `finally`. |
| `frontend/vite.showcase.config.ts`, `frontend/showcase/index.html` | static build/security configuration | Relative assets work below a Pages repository path. Standalone output is separate from wheel dashboard assets. The demo's content security policy denies network connections and embedded frames while allowing bundled Graphviz WebAssembly. |

The demo reuses production views; it illustrates presentation and navigation. It
does not test PostgreSQL transactions, worker isolation, provider behavior or
formal proof validity. The frontend file paths above are relative to
`src/archon_horizon/`.

## Validation evidence

- Development inventory tests pass for nested scope, branch attribution,
  explicit review evidence, unknown/observed line coverage and syntax gaps.
- `generate_dashboard_fixture.py --check` matches the committed JSON.
- Frontend typecheck, demo transport tests and production demo build pass.
- Playwright passes project → objective → nodes → node DAG → reload →
  references → Activity → Markdown goal ledger below `/Archon-Horizon/demo/`,
  plus a legacy bare node link and final-page session pagination exhaustion.
- The browser observes no API or external network requests. A direct attempted
  mutation receives 403. Synthetic viewer permissions hide editing/cancel actions.
- MkDocs builds with `--strict`; the assembled site includes the standalone demo.
- Release-version regressions cover alpha → beta → stable badges and malformed
  README/lockfile inputs preserving all version surfaces before validation ends.

GitHub Pages publication and a live deployment are separate operator actions.
The workflow builds review artifacts automatically and deploys only a manually
requested build of `main`; no site was published during this development review.
