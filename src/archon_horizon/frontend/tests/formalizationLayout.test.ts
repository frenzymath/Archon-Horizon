import assert from "node:assert/strict";
import { test } from "node:test";
import { buildFormalizationGraph, formalizationDot } from "../src/formalizationGraph";
import { cachedLayout, layoutDot } from "../src/vizInstance";

test("graph layouts share one worker and pending work, cache results, and retry failures", async (context) => {
  const originalWorker = globalThis.Worker;
  class LayoutWorker {
    static instances: LayoutWorker[] = [];
    onmessage?: (event: { data: { id: number; svg?: string; error?: string } }) => void;
    onerror?: () => void;
    requests: Array<{ id: number; dot: string }> = [];
    constructor() { LayoutWorker.instances.push(this); }
    postMessage(request: { id: number; dot: string }) { this.requests.push(request); }
    finish(index: number, svg: string) { this.onmessage?.({ data: { id: this.requests[index].id, svg } }); }
    fail(index: number) { this.onmessage?.({ data: { id: this.requests[index].id, error: "Invalid DOT" } }); }
  }
  globalThis.Worker = LayoutWorker as unknown as typeof Worker;
  context.after(() => { globalThis.Worker = originalWorker; });
  const graph = { nodes: [{ id: "root", title: "Root", children: ["leaf"] }, { id: "leaf", title: "Leaf" }], proposals: [{ id: "legacy", claim_id: "root", children: [] }] };
  const dot = formalizationDot(buildFormalizationGraph(graph, "root"));
  const prepared = layoutDot(dot);
  assert.equal(LayoutWorker.instances.length, 1);
  const worker = LayoutWorker.instances[0];
  const opened = layoutDot(dot);
  assert.equal(prepared, opened);
  assert.equal(layoutDot(formalizationDot(buildFormalizationGraph({ ...graph }, "root"))), prepared);
  assert.equal(worker.requests.length, 1);
  assert.equal(cachedLayout(dot), undefined);
  worker.finish(0, '<svg data-layout="prepared" />');
  await prepared;
  assert.equal(cachedLayout(dot), '<svg data-layout="prepared" />');
  assert.equal(await layoutDot(dot), cachedLayout(dot));
  assert.equal(worker.requests.length, 1);

  const revised = { ...graph, nodes: [{ id: "root", title: "Root", children: [] }, graph.nodes[1]] };
  const revision = layoutDot(formalizationDot(buildFormalizationGraph(revised, "root")));
  assert.equal(worker.requests.length, 2);
  assert.notEqual(worker.requests[1].dot, dot);
  worker.finish(1, '<svg data-layout="revised" />');
  await revision;
  assert.equal(worker.requests.length, 2);

  const failed = layoutDot("invalid graph");
  worker.fail(2);
  await assert.rejects(failed, /Invalid DOT/);
  const retry = layoutDot("invalid graph");
  assert.notEqual(retry, failed);
  assert.equal(worker.requests.length, 4);
  worker.finish(3, "<svg />");
  await retry;
});
