import assert from "node:assert/strict";
import { ApiError, request } from "../src/pipeline/api";

let calls = 0;
let timers: number[] = [];
const realTimeout = globalThis.setTimeout;
globalThis.window = {
  setTimeout: (callback: () => void, ms: number) => {
    timers.push(ms);
    return realTimeout(callback, ms >= 15000 ? 10 : 0);
  },
  clearTimeout: globalThis.clearTimeout,
} as unknown as Window & typeof globalThis;

async function main() {
  globalThis.fetch = async () => {
    calls++;
    if (calls === 1) throw new TypeError("Network disconnected");
    if (calls === 2) return new Response("{}", {status: 503});
    return Response.json({ok: true});
  };
  assert.deepEqual(await request("/projects"), {ok: true});
  assert.equal(calls, 3);
  assert.equal(timers.filter(ms => ms === 30000).length, 3);

  calls = 0;
  globalThis.fetch = async () => { calls++; throw new TypeError("Network disconnected"); };
  await assert.rejects(request("/commands", {method: "POST", body: "{}"}), TypeError);
  assert.equal(calls, 1, "Uncertain mutations must not be replayed");

  calls = 0;
  globalThis.fetch = async () => { calls++; return new Response("{}", {status: 401}); };
  await assert.rejects(request("/auth/me"), error => error instanceof ApiError && error.status === 401);
  assert.equal(calls, 1);

  calls = 0;
  globalThis.fetch = async (_url, init) => {
    calls++;
    return new Promise((_resolve, reject) => {
      init!.signal!.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")), {once: true});
    });
  };
  await assert.rejects(request("/dashboard/activity/runs/1"), error =>
    error instanceof ApiError && error.status === 408 && error.message.includes("connection timed out"));
  assert.equal(calls, 3);

  calls = 0;
  const controller = new AbortController();
  const cancelled = request("/projects", {signal: controller.signal});
  controller.abort();
  await assert.rejects(cancelled, error => error instanceof DOMException && error.name === "AbortError");
  assert.equal(calls, 1, "Navigation cancels without retrying");

  calls = 0;
  const backoff = new AbortController();
  globalThis.fetch = async () => {
    calls++;
    queueMicrotask(() => backoff.abort());
    throw new TypeError("offline");
  };
  await assert.rejects(request("/projects", {signal: backoff.signal}));
  assert.equal(calls, 1, "Cancellation during recovery must not start another read");
  console.log("Pipeline transport: retry, timeout, authentication, mutation and cancellation checks passed");
}
void main().catch(error => { console.error(error); process.exitCode = 1; });
