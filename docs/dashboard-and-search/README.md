# Live Dashboard & Offline Declaration Search

Archon Horizon includes both an interactive Single Page Application (SPA) dashboard for monitoring workspace progress and a high-performance offline search engine for querying Lean 4 declarations.

---

## Table of Contents

- [1. Live Web Dashboard Server](#1-live-web-dashboard-server)
  - [Clickable Local-Reference Chips](#clickable-local-reference-chips)
  - [Board View (`/board`)](#board-view-board)
  - [Commit Resolution API & Log Ordering](#commit-resolution-api--log-ordering)
- [2. Static Snapshot Export & GitHub Pages](#2-static-snapshot-export--github-pages)
  - [Automated GitHub Pages Workflow](#automated-github-pages-workflow)
- [3. Offline Lean Declaration Search (`horizon search`)](#3-offline-lean-declaration-search-horizon-search)
  - [Three Search Modes](#three-search-modes)
  - [Index Maintenance](#index-maintenance)

---

## 1. Live Web Dashboard Server

To launch the live dashboard server, run:

```bash
horizon dashboard --host 127.0.0.1 --port 8765
```

On a remote server, container, or port-forwarded environment where loopback
forwarding is unreliable, use:

```bash
horizon dashboard --public --port 8765
```

`--public` binds the server to `0.0.0.0` (all IPv4 interfaces). Open the
dashboard through your tunnel as `http://localhost:8765/`, or directly as
`http://<server-host>:8765/` when the network and firewall allow it.

`horizon run` starts the same live dashboard by default while a run is active.
Pass `--host`, `--port`, or `--public` to `horizon run` to control that server,
or `--no-dashboard` to run without the UI.

The server is implemented in [`server/app.py`](../../src/archon_horizon/server/app.py) (with data assembled by [`server/service.py`](../../src/archon_horizon/server/service.py) and Git/source endpoints in [`server/git_api.py`](../../src/archon_horizon/server/git_api.py) and [`server/source_api.py`](../../src/archon_horizon/server/source_api.py)); the CLI entry point is [`commands/dashboard.py`](../../src/archon_horizon/commands/dashboard.py), and the SPA source lives under [`frontend/`](../../src/archon_horizon/frontend). It provides real-time visualization and management features:
- **Interactive chapter graph**: Explore a deterministic Graphviz layout with chapters collapsed by default, one-chapter expansion, filtering, and node selection.
- **Roadmap & Task Tracker**: Monitor ongoing execution runs, inspect step-by-step run logs, and view milestone progress.
- **Board view**: A GitHub-Projects-style kanban that groups the roadmap by milestone (see [Board View](#board-view-board)).
- **Inbox Management**: Directly review, triage, and comment on workspace inbox hints and issues.
- **Clickable reference chips**: Ids and commit SHAs mentioned anywhere in rendered text become links that jump straight to the entity (see [Clickable Local-Reference Chips](#clickable-local-reference-chips)).

### Clickable Local-Reference Chips

Rendered prose across the dashboard — inbox bodies, commit messages, session reports, roadmap summaries, and comments — now turns bare references to local entities into clickable, hoverable tag chips that navigate to the matching view. Recognized tokens are roadmap item ids, task ids, inbox ids, blueprint/hgraph node keys, and commit SHAs.

This is **linkify-by-known-set**, not pattern-guessing: a token becomes a chip only when it resolves against the set of ids already in app state ([`refs.tsx`](../../src/archon_horizon/frontend/src/refs.tsx) builds the resolver keyed on live roadmap/task/inbox/node ids), so ordinary prose can't sprout false links. Commit SHAs are gated further — a hex token is only linkified when it matches a known `pinned_commits` entry (matched on its first 7 characters, so both abbreviated and full forms resolve). The rendered-markdown pass that injects the chip anchors lives in [`components/MarkdownBlock.tsx`](../../src/archon_horizon/frontend/src/components/MarkdownBlock.tsx) (`linkifyRefs`), which walks the assembled inline HTML and skips text already inside `<a>`, `<code>`, or `<pre>`. A commit chip self-resolves its subject and owning project from the live `/api/commit` endpoint (see below), so a pinned SHA reads as `abc1234 · fix the thing`.

### Board View (`/board`)

The **Board** page ([`BoardPage.tsx`](../../src/archon_horizon/frontend/src/BoardPage.tsx), reachable from the header nav) is a GitHub-Projects-style view of the roadmap that serves agents and humans off the exact same data. It groups roadmap items by their `milestone` label (a free string, with `Unassigned` floated to the end), lays out kanban columns by status (active / pending / blocked / done / rejected), and surfaces each item's `@owner` chip. A derived **live "running" pulse** marks any item a currently running task points at through its `roadmap_refs` — this is computed, never stored. Each card can expand to show projects, dependencies (as reference chips), the item's milestone, and its **pinned-commit chips**.

### Commit Resolution API & Log Ordering

- **`GET /api/commit?sha=<sha>`** resolves a bare (possibly abbreviated) commit SHA across every project to `{sha, short_sha, subject, project}`, returning the first match or `404` when nothing matches so the client leaves the reference plain. It backs the commit chips described above. Implemented in [`server/service.py`](../../src/archon_horizon/server/service.py) (`_resolve_commit`, dispatched from the `/api/commit` path handler).
- **Commit log order**: the per-session commit log is now sorted **latest-first** — [`session_commits_view`](../../src/archon_horizon/server/service.py) sorts its rows by commit date descending before returning them.

---

## 2. Static Snapshot Export & GitHub Pages

For public reporting or static documentation hosting, Horizon can export a self-contained static HTML snapshot of the entire dashboard (see [`render/static_export.py`](../../src/archon_horizon/render/static_export.py) and [`render/dashboard.py`](../../src/archon_horizon/render/dashboard.py)):

```bash
# Build the SPA first so the export includes the interactive app (with run logs):
npm --prefix src/archon_horizon/frontend install && npm --prefix src/archon_horizon/frontend run build
horizon dashboard --static --out ./public_dashboard --dist src/archon_horizon/frontend/dist
```

The export writes one `data/api/<sha256(path)>.json` per endpoint — **including every session's transcript and report**, so the run logs travel with the snapshot. In static mode the SPA redirects `/api/*` fetches to those files, resolved against the page's directory so it works whether the page is opened at `…/repo/` or `…/repo/index.html`.

> [!IMPORTANT]
> The static logs viewer only renders when a **built SPA** is supplied via `--dist` (or shipped in the installed package — `install.sh` builds it for you). Without one, the export falls back to a read-only single-file page that omits the run logs.

### Automated GitHub Pages Workflow
You can automatically generate a GitHub Actions workflow that publishes your static dashboard snapshot directly to GitHub Pages whenever changes are pushed:

```bash
horizon dashboard --static --out ./docs_site --workflow
```

This writes a ready-to-use continuous deployment pipeline to `.github/workflows/`, allowing automated publication to `github.io`.

The generated workflow retries the GitHub Pages deploy up to three times within a single run, because GitHub's Pages backend intermittently returns `Deployment failed, try again later.` for a valid upload even while Pages is fully operational. **Do not manually re-run a failed deploy** — a re-run uploads a second `github-pages` artifact onto the same run and makes `actions/deploy-pages` fail with *"Multiple artifacts named github-pages"*. Let the next push (or the retry logic) redeploy instead.

### Freshness & caching
GitHub Pages serves every file — `index.html`, the JS bundle, and each `data/api/<hash>.json` — with `Cache-Control: max-age=600` (10 minutes), and you can't change those response headers on Pages. Because the data filenames hash the API **path, not the payload**, each snapshot reuses the same URLs, so a naive browser would keep showing up-to-10-minute-old runs.

To avoid that, the static-mode fetch shim ([`staticMode.ts`](../../src/archon_horizon/frontend/src/staticMode.ts)) requests the data files with `cache: 'no-cache'`, forcing a conditional request on every load: a cheap `304 Not Modified` when the snapshot is unchanged, a fresh `200` when new data was published. As a result a normal reload always shows current run data — no hard refresh or cache-clearing needed.

Two caveats remain: (1) the page **shell** (HTML/JS) is still subject to the 10-minute Pages cache, so app-code changes can take up to ~10 min to appear; and (2) a hard reload does **not** reliably help stale JS-issued `fetch()` data — especially in Safari, whose "Reload From Origin" (⌘⌥R) bypasses cache for the document and its subresources but applies the default cache mode to programmatic `fetch()`. The `no-cache` shim is what makes the data current, not the keyboard shortcut.

---

## 3. Offline Lean Declaration Search (`horizon search`)

Finding the right lemma in Mathlib or local member projects is crucial during formalization. `horizon search` builds and queries an offline index directly from `.lean` source files on disk—requiring zero GPU resources, API keys, or network connectivity. The index is built in [`search/index.py`](../../src/archon_horizon/search/index.py), workspace-wide crawling in [`search/workspace.py`](../../src/archon_horizon/search/workspace.py), and the CLI in [`commands/search.py`](../../src/archon_horizon/commands/search.py).

### Three Search Modes

| Mode | Command Example | Description |
| :--- | :--- | :--- |
| **Natural Language (BM25)** | `horizon search "continuous function on compact set"` | Scores lemmas based on combined docstrings, names, and type signatures. |
| **Name Match (Loogle-style)** | `horizon search --name "Continuous.compact"` | Fast substring and exact matching against declaration identifiers. |
| **Signature Pattern (`--type`)** | `horizon search --type "?a -> ?b -> ?a"` | Heuristic signature matching where `?a` and `_` act as type wildcards. |

### Index Maintenance
If you update external libraries or compile new Mathlib files via `lake build`, rebuild your cached index with:

```bash
horizon search --reindex
```
