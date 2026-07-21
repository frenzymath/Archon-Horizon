let worker: Worker | null = null;
let workerFailed = false;
let sequence = 0;
const pending = new Map<number, { resolve: (svg: string) => void; reject: (error: Error) => void }>();
const cache = new Map<string, string>();
const CACHE_MAX = 200;

function remember(dot: string, svg: string): string {
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

async function layoutOnMainThread(dot: string): Promise<string> {
  const { instance } = await import('@viz-js/viz');
  const viz = await instance();
  return remember(dot, viz.renderString(dot, { format: 'svg' }));
}

export function layoutDot(dot: string): Promise<string> {
  const hit = cache.get(dot);
  if (hit) return Promise.resolve(hit);
  const activeWorker = graphvizWorker();
  if (!activeWorker) return layoutOnMainThread(dot);
  return new Promise<string>((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, { resolve: (svg) => resolve(remember(dot, svg)), reject });
    activeWorker.postMessage({ id, dot });
  }).catch((error) => workerFailed ? layoutOnMainThread(dot) : Promise.reject(error));
}

let prefetchToken = 0;
export function prefetchLayouts(dots: string[]): void {
  const token = ++prefetchToken;
  const queue = dots.filter((dot) => !cache.has(dot));
  const later = (callback: () => void) => typeof requestIdleCallback === 'function'
    ? requestIdleCallback(callback, { timeout: 4000 })
    : window.setTimeout(callback, 300);
  const next = () => {
    if (token !== prefetchToken) return;
    const dot = queue.shift();
    if (!dot) return;
    layoutDot(dot).catch(() => {}).finally(() => later(next));
  };
  later(next);
}
