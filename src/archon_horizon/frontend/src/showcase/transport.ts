import fixture from './fixture.json';
import { nodeHasLabel } from '../formalizationGraph';

const page = (items: unknown[]) => ({items, next_cursor: null});
const prefix = `/projects/${fixture.project.id}/dashboard`;

/** Resolve only synthetic reads; unknown routes fail visibly instead of contacting a server. */
export function demoResponse(input: string, method = 'GET'): Response {
  const url = new URL(input, 'https://demo.invalid');
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
    status, headers: {'Content-Type': 'application/json'},
  });
  if (method.toUpperCase() !== 'GET') return json({error: {message: 'This synthetic demo is read-only.'}}, 403);
  if (!url.pathname.startsWith('/api/v3/')) return json({error: {message: 'The demo has no network transport.'}}, 404);
  const route = url.pathname.slice('/api/v3'.length);
  if (route === '/auth/me') return json({id: 'demo-viewer', username: 'demo', role: 'viewer', permissions: {admin: false, write: false}});
  if (route === '/projects') return json(page([fixture.project]));
  if (route === `/projects/${fixture.project.id}`) return json(fixture.project);
  if (route === `${prefix}/overview`) return json({...fixture.project, markdown: `# Finite sums\n\n${fixture.project.description}\n\nExplore **Objectives**, **Nodes**, the node **DAG**, **References**, and the **Activity** view.\n\nAll data in this demo is synthetic. Editing and integrations are disabled.`});
  if (route === `${prefix}/objectives`) return json(page([fixture.objective]));
  if (route === `${prefix}/objectives/${fixture.objective.id}`) return json(fixture.objective);
  if (route === `${prefix}/graph-targets`) return json({target_repository_id: 'demo-workspace', targets: [{id: 'demo-workspace', slug: 'workspace', purpose: 'workspace'}]});
  if (route === `${prefix}/graph`) return json({nodes: fixture.nodes, scope: 'project'});
  if (route === `${prefix}/nodes/resolve`) return json({nodes: fixture.nodes});
  if (route === `${prefix}/nodes`) {
    const query = (url.searchParams.get('search') || '').toLowerCase();
    const label = url.searchParams.get('label');
    const type = url.searchParams.get('node_type');
    const milestone = url.searchParams.get('milestone');
    const nodes = fixture.nodes.filter(node => `${node.title} ${node.label}`.toLowerCase().includes(query)
      && (!label || nodeHasLabel(node, label)) && (!type || node.kind === type)
      && (!milestone || nodeHasLabel(node, 'milestone') === (milestone === 'true')));
    const offset = Number(url.searchParams.get('offset') || 0), limit = Number(url.searchParams.get('limit') || 50);
    return json({nodes: nodes.slice(offset, offset + limit), total: nodes.length, types: [...new Set(fixture.nodes.map(node => node.kind))].sort()});
  }
  if (route.startsWith(`${prefix}/nodes/`)) {
    const node = fixture.nodes.find(item => item.id === decodeURIComponent(route.slice(`${prefix}/nodes/`.length)));
    return node ? json({node, nodes: fixture.nodes}) : json({error: {message: 'Unknown demo node.'}}, 404);
  }
  if (route === `/projects/${fixture.project.id}/integrations`) return json(page([]));
  if (route === `${prefix}/missions`) return json({items: [], match_ids: [], total: 0, next_offset: null});
  if (route === '/dashboard/activity/runs') {
    const search = (url.searchParams.get('search') || '').toLowerCase();
    const status = url.searchParams.get('status');
    return json({runs: fixture.run.title.toLowerCase().includes(search) && (!status || status === fixture.run.status) ? [fixture.run] : [], next_before: null});
  }
  if (route === `/dashboard/activity/runs/${fixture.run.id}`) {
    if (url.searchParams.get('view') === 'queue') return json({admissions: Object.fromEntries(fixture.sessions.map(session => [session.id, session.context.admission]))});
    const older = url.searchParams.has('sessions_before');
    return json({...fixture.run, sessions: older ? fixture.sessions.slice(2) : fixture.sessions.slice(0, 2),
      sessions_next_before: older ? null : 'demo-before'});
  }
  const sessionPath = '/dashboard/activity/sessions/';
  if (route.startsWith(sessionPath)) {
    const [id, suffix] = route.slice(sessionPath.length).split('/');
    const session = fixture.sessions.find(item => item.id === id);
    if (!session) return json({error: {message: 'Unknown demo session.'}}, 404);
    if (suffix === 'events') return json({events: fixture.events, next_before: null});
    if (suffix === 'logs') return json({enabled: false, records: []});
    if (url.searchParams.get('view') === 'reports') return json({reports: [fixture.report]});
    return json({...session, attempts: [], reports: [fixture.report], events: fixture.events, next_before: null});
  }
  if (route === '/references') {
    const query = (url.searchParams.get('q') || '').toLowerCase();
    return json(page(fixture.references.filter(item => `${item.title} ${item.cite_key} ${item.authors.join(' ')}`.toLowerCase().includes(query))));
  }
  if (route === `/records/reference/${fixture.references[0].id}`) return json(fixture.references[0]);
  if (route === `/references/${fixture.references[0].id}/bibtex`) return new Response(fixture.bibtex, {headers: {'Content-Type': 'text/plain'}});
  if (/^\/references\/[^/]+\/files$/.test(route)) return json({items: [], next_cursor: null, max_upload_bytes: 67108864});
  if (route.startsWith('/records/')) return json(page([]));
  if (route === '/resources') return json({hosts: [], storage: [], free_bytes: null, observed_at: fixture.generated_at, oldest_pending_delivery: null, backup_status: 'demo', provider_status: 'demo'});
  return json({error: {message: `This view is outside the synthetic demo: ${route}`}}, 404);
}

/** Install transport before the production dashboard mounts, including its event stream. */
export function installDemoTransport() {
  window.fetch = async (input, init) => {
    const request = input instanceof Request ? input : null;
    if (init?.signal?.aborted || request?.signal.aborted) throw new DOMException('Aborted', 'AbortError');
    return demoResponse(request?.url || String(input), init?.method || request?.method || 'GET');
  };
  // An in-memory event source allows the native connection indicator to settle
  // without polling, a service worker, or a real event-stream connection.
  class DemoEvents extends EventTarget {
    onopen: ((event: Event) => void) | null = null;
    onmessage: ((event: MessageEvent) => void) | null = null;
    onerror: ((event: Event) => void) | null = null;
    readyState = 0;
    private closed = false;
    constructor(_url: string | URL) {
      super();
      queueMicrotask(() => {if (!this.closed) {this.readyState = 1; this.onopen?.(new Event('open'));}});
    }
    close() {this.closed = true; this.readyState = 2;}
  }
  window.EventSource = DemoEvents as unknown as typeof EventSource;
}

/** Keep production /pipeline links inside the standalone demo's current path. */
export function installDemoNavigation() {
  const path = location.pathname;
  for (const name of ['pushState', 'replaceState'] as const) {
    const original = history[name].bind(history);
    history[name] = (data, unused, target) => {
      const next = target == null ? null : new URL(String(target), location.href);
      if (next?.origin === location.origin && next.pathname === '/pipeline') next.pathname = path;
      original(data, unused, next);
    };
  }
  document.addEventListener('click', event => {
    const anchor = (event.target as Element | null)?.closest('a');
    if (!anchor) return;
    const next = new URL(anchor.href, location.href);
    if (next.origin !== location.origin || next.pathname.startsWith('/api/')) {
      event.preventDefault();
      return;
    }
    if (next.pathname === '/pipeline') {event.preventDefault(); next.pathname = path; location.assign(next.href);}
  }, true);
}
