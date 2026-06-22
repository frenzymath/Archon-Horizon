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
        return orig(`./data/api/${key}.json`, init);
      }
    } catch {
      /* fall through */
    }
    return orig(input as RequestInfo, init);
  }) as typeof window.fetch;
}

export {};
