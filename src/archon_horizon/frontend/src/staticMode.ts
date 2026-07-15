// Dual-mode data source. In live mode the app fetches /api/* from the Python
// server. In static mode (github.io), a `window.__ARCHON_STATIC__` marker is
// injected at export time and we monkey-patch fetch to redirect /api/<path>
// to ./data/api/<sha256(path)>.json — files the Python exporter wrote with the
// SAME hash (hashlib.sha256(path).hexdigest()). One app, two backends.

declare global {
  interface Window {
    __ARCHON_STATIC__?: { generatedAt?: string; endpointCount?: number };
  }
}

export function isStaticDashboard(): boolean {
  return !!window.__ARCHON_STATIC__;
}

async function sha256Hex(path: string): Promise<string> {
  const bytes = new TextEncoder().encode(path);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

export function installStaticFetch(): void {
  if (!isStaticDashboard()) return;
  const orig = window.fetch.bind(window);
  window.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const raw = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
    try {
      const url = new URL(raw, window.location.href);
      if (url.pathname.startsWith('/api/')) {
        const key = await sha256Hex(url.pathname + url.search);
        // Resolve the exported files against the page's *directory*. On GitHub
        // Pages the app is served from a subpath (…/Archon-Horizon/), and the
        // entry URL may be the directory itself (…/) or an explicit …/index.html.
        // Strip everything after the last '/' so both resolve to the same base;
        // just appending '/' would turn '…/index.html' into '…/index.html/' and
        // 404 every fetch. (Static mode uses HashRouter, so in-app routes live in
        // the hash and never change the pathname.)
        const p = window.location.pathname;
        const basePath = p.endsWith('/') ? p : p.slice(0, p.lastIndexOf('/') + 1);
        // The exported JSON is content-stable by URL (the filename hashes the
        // API *path*, not the payload) and GitHub Pages serves everything with
        // Cache-Control: max-age=600. Without an override the browser would keep
        // showing up-to-10-min-old data, and a hard reload wouldn't fix it since
        // it doesn't reliably bypass cache for JS-issued fetch() (esp. Safari).
        // 'no-cache' forces a conditional request every load: cheap 304 when the
        // data is unchanged, fresh 200 when a new snapshot was published.
        return orig(`${basePath}data/api/${key}.json`, { ...init, cache: 'no-cache' });
      }
    } catch {
      /* fall through */
    }
    return orig(input as RequestInfo, init);
  }) as typeof window.fetch;
}

export {};
