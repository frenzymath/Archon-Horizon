let worker: Worker | null = null;
let workerFailed = false;
let sequence = 0;
const pending = new Map<number, { resolve: (svg: string) => void; reject: (error: Error) => void }>();
const inFlight = new Map<string, Promise<string>>();
const cache = new Map<string, string>();
const CACHE_MAX = 200;

function remember(dot: string, svg: string): string {
  cache.delete(dot);
  if (cache.size >= CACHE_MAX) {
    const oldest = cache.keys().next().value;
    if (oldest !== undefined) cache.delete(oldest);
  }
  cache.set(dot, svg);
  return svg;
}

export const cachedLayout = (dot: string) => cache.get(dot);

function graphvizWorker(): Worker | null {
  if (workerFailed) return null;
  if (!worker) {
    try {
      worker = new Worker(new URL('./vizWorker.ts', import.meta.url), { type: 'module' });
      worker.onmessage = (event: MessageEvent<{ id: number; svg?: string; error?: string }>) => {
        const request = pending.get(event.data.id);
        if (!request) return;
        pending.delete(event.data.id);
        if (event.data.svg != null) request.resolve(event.data.svg);
        else request.reject(new Error(event.data.error || 'Graphviz layout failed'));
      };
      worker.onerror = () => {
        workerFailed = true;
        pending.forEach((request) => request.reject(new Error('Graphviz worker failed')));
        pending.clear();
        worker?.terminate();
        worker = null;
      };
    } catch {
      workerFailed = true;
    }
  }
  return worker;
}

let mainThreadInstance: ReturnType<typeof import('@viz-js/viz')['instance']> | undefined;
async function layoutOnMainThread(dot: string): Promise<string> {
  mainThreadInstance ??= import('@viz-js/viz').then(({ instance }) => instance());
  const viz = await mainThreadInstance;
  return remember(dot, viz.renderString(dot, { format: 'svg' }));
}

export function layoutDot(dot: string): Promise<string> {
  const hit = cache.get(dot);
  if (hit) return Promise.resolve(hit);
  const active = inFlight.get(dot);
  if (active) return active;
  const activeWorker = graphvizWorker();
  const layout = !activeWorker ? layoutOnMainThread(dot) : new Promise<string>((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve: (svg) => resolve(remember(dot, svg)), reject });
    try { activeWorker.postMessage({ id, dot }); }
    catch (error) { pending.delete(id); reject(error); }
  }).catch((error) => workerFailed ? layoutOnMainThread(dot) : Promise.reject(error));
  const result = layout.finally(() => inFlight.delete(dot));
  inFlight.set(dot, result);
  return result;
}
