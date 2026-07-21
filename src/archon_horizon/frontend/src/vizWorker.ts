import { instance } from '@viz-js/viz';

const viz = instance();

self.onmessage = async (event: MessageEvent<{ id: number; dot: string }>) => {
  try {
    const renderer = await viz;
    postMessage({ id: event.data.id, svg: renderer.renderString(event.data.dot, { format: 'svg' }) });
  } catch (error) {
    postMessage({ id: event.data.id, error: String(error) });
  }
};
