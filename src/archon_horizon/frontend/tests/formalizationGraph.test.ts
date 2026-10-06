import assert from "node:assert/strict";
import { test } from "node:test";
import { instance } from "@viz-js/viz";
import { buildFormalizationGraph, fitGraphTransform, formalizationDot, nodeGraphStatus, nodeHasLabel, nodeLabels, nodeOpenPullCount, zoomGraphAt } from "../src/formalizationGraph";

test("node graph uses node children and ignores legacy proposal dependencies", () => {
  const model = buildFormalizationGraph({
    nodes: [{ id: "goal", kind: "claim", children: ["foundation", "foundation"] }, { id: "foundation", kind: "definition", children: ["leaf"] }, { id: "leaf" }, { id: "alternative" }, { id: "unrelated", children: ["goal"] }],
    routes: [{ id: "r1", parent_claim_id: "goal", children: [{ claim_id: "leaf", revision: 1 }, { claim_id: "leaf", revision: 1 }] }, { id: "r2", parent_claim_id: "goal", children: ["alternative"] }],
    proposals: [{ id: "p1", claim_id: "goal", route_id: "r1", title: "First proof" }, { id: "p2", claim_id: "goal", route_id: "r2", title: "Alternative proof" }],
  }, "goal");
  assert.equal(model.vertices.length, 3);
  assert.ok(!model.byKey.has("node:unrelated"));
  assert.ok(!model.byKey.has("node:alternative"));
  assert.deepEqual(model.edges, [["node:goal", "node:foundation"], ["node:foundation", "node:leaf"]]);
  assert.equal(model.vertices.every((vertex) => vertex.type === "node"), true);
  assert.equal(model.hasCycle, false);
});

test("explicit metadata children are followed and unresolved children remain visible", () => {
  const model = buildFormalizationGraph({ nodes: [{ id: "root", metadata: { children: ["a", { claim_id: "missing", revision: 3 }] } }, { id: "a", markdown: "[[node:not-a-dependency]]" }, { id: "not-a-dependency" }] }, "root");
  assert.equal(model.vertices.length, 3);
  assert.equal(model.byKey.get("node:missing")?.missing, true);
  assert.ok(!model.byKey.has("node:not-a-dependency"));
});

test("objective mentions select independent nodes without fabricating dependencies", () => {
  const model = buildFormalizationGraph({
    nodes: [{ id: "root" }, { id: "stage", label: "stage-theorem", children: ["green"] }, { id: "green", status: "formalized" }],
    scope: "objective", referenced_nodes: ["root", "stage"],
  }, "root");
  assert.deepEqual(model.edges, [["node:stage", "node:green"]]);
  assert.equal(model.vertices.length, 3);
  assert.equal(nodeGraphStatus(model.byKey.get("node:green")!.item), "formalized");
});

test("hiding definitions preserves visible descendants and the selected root", () => {
  const model = buildFormalizationGraph({ nodes: [{ id: "root", kind: "definition", children: ["hidden"] }, { id: "hidden", kind: "definition", children: ["leaf"] }, { id: "leaf", kind: "claim" }] }, "root", { showDefinitions: false, showArchived: false });
  assert.deepEqual(model.vertices.map((vertex) => vertex.item.id), ["root", "leaf"]);
  assert.deepEqual(model.edges, [["node:root", "node:leaf"]]);
  assert.equal(model.hiddenCount, 1);
});

test("archived node dependencies appear only when requested", () => {
  const graph = { nodes: [{ id: "root", children: ["leaf", "obsolete"] }, { id: "leaf" }, { id: "obsolete", lifecycle: "archived" }] };
  assert.equal(buildFormalizationGraph(graph, "root").vertices.length, 2);
  const archived = buildFormalizationGraph(graph, "root", { showDefinitions: true, showArchived: true });
  assert.equal(archived.vertices.length, 3);
});

test("legacy proposal and route data cannot add node dependencies", () => {
  const graph = {
    nodes: [{ id: "root", children: ["own"] }, { id: "own" }, { id: "legacy" }, { id: "independent" }],
    routes: [{ id: "old", parent_claim_id: "root", children: ["legacy"], stale: true }],
    proposals: [
      { id: "direct", claim_id: "root", route_id: "old", children: [], markdown: "Independent direct proof." },
      { id: "own", claim_id: "root", metadata: { children: ["own", "own"] } },
      { id: "other", claim_id: "independent", children: ["root"] },
    ],
  };
  const model = buildFormalizationGraph(graph, "root");
  assert.deepEqual(model.edges, [["node:root", "node:own"]]);
  assert.ok(!model.byKey.has("node:legacy"));
  assert.ok(!model.byKey.has("independent"));
});

test("cycles and deep dependencies terminate without recursion and are reported honestly", () => {
  const cyclic = buildFormalizationGraph({ nodes: [{ id: "root", children: ["child"] }, { id: "child", children: ["root"] }] }, "root");
  assert.equal(cyclic.hasCycle, true);
  assert.equal(cyclic.vertices.length, 2);
  const nodes = Array.from({ length: 12000 }, (_, index) => ({ id: `n${index}`, children: index < 11999 ? [`n${index + 1}`] : [] }));
  const deep = buildFormalizationGraph({ nodes }, "n0");
  assert.equal(deep.vertices.length, 12000);
  assert.equal(deep.hasCycle, false);
  assert.equal(buildFormalizationGraph({ nodes }, "absent").vertices.length, 0);
});

test("Graphviz receives escaped labels and renders node dependencies", async () => {
  const malicious = 'Goal "]; forged -> "node\n<script>alert(1)</script>';
  const model = buildFormalizationGraph({ nodes: [{ id: 'root"\n', title: malicious, kind: "claim", children: ["def"] }, { id: "def", title: "Definition", kind: "definition" }] }, 'root"\n');
  const dot = formalizationDot(model);
  const renderer = await instance();
  const svg = renderer.renderString(dot, { format: "svg" });
  assert.equal((svg.match(/class="node"/g) || []).length, 2);
  assert.match(dot, /shape=ellipse/);
  assert.match(dot, /shape=box/);
  assert.doesNotMatch(dot, /shape=diamond/);
  assert.doesNotMatch(svg, /<script>/);
  assert.match(dot, /v1 -> v0;/);
});

test("simultaneous node labels and open PRs appear on the DAG", () => {
  assert.equal(nodeGraphStatus({ stage: "formally_proved" }), "formalized");
  assert.equal(nodeGraphStatus({ completion: { statement_aligned: true } }), "statement-aligned");
  assert.equal(nodeGraphStatus({ completion: { current_kernel_checked: true } }), "candidate");
  assert.deepEqual(nodeLabels({ stage: "formally_stated", labels: ["informal_stated", "proof_sketch", "potentially_outdated"] }),
    ["informal_stated", "proof_sketch", "formally_stated"]);
  assert.equal(nodeOpenPullCount({ open_pull_count: 2 }), 2);
  const model = buildFormalizationGraph({
    nodes: [{ id: "root", title: "Root", labels: ["informal_stated", "proof_sketch"], open_pull_count: 2 }],
  }, "root");
  const dot = formalizationDot(model);
  assert.match(dot, /Informal Stated/);
  assert.match(dot, /Proof\\nSketch/);
  assert.match(dot, /2 PRs/);
  assert.match(dot, /fillcolor="#f6ebcf"/);
});

test("the milestone label adds an orthogonal double outline without changing progress labels", () => {
  const milestone = { id: "m05", title: "M05: Hamilton-Ivey pinching", status: "open", labels: ["milestone"] };
  assert.equal(nodeHasLabel(milestone, "milestone"), true);
  assert.equal(nodeGraphStatus(milestone), "open");
  const dot = formalizationDot(buildFormalizationGraph({ nodes: [milestone] }, milestone.id));
  assert.match(dot, /color="#2f789b"/);
  assert.match(dot, /peripheries=2/);
  assert.equal(nodeHasLabel({ metadata: { labels: ["Milestone"] } }, "milestone"), true);
  assert.equal(nodeHasLabel({ labels: ["reviewed"] }, "milestone"), false);
});

test("graph colors follow the backend formalized status without requiring a separate attestation", () => {
  assert.equal(nodeGraphStatus({ status: "candidate", validation_checks: { kernel: "pass" } }), "candidate");
  assert.equal(nodeGraphStatus({ status: "proved", metadata: { formally_verified: true } }), "open");
  assert.equal(nodeGraphStatus({ status: "formalized", formally_verified: false }), "formalized");
  const model = buildFormalizationGraph({ nodes: [{ id: "root", status: "formalized", formally_verified: false }] }, "root");
  assert.match(formalizationDot(model), /color="#13663e"/);
  assert.equal(nodeGraphStatus({ formally_verified: true }), "open");
  assert.equal(nodeGraphStatus({ formally_verified: true, stale: true }), "stale");
});

test("recorded current checks render as candidates without assigning formally_proved", () => {
  const node = { id: "checked", status: "open", completion: {
    current_kernel_checked: true, source_reported_complete: true, exact_claim_closed: false,
  } };
  assert.equal(nodeGraphStatus(node), "candidate");
  const dot = formalizationDot(buildFormalizationGraph({ nodes: [node] }, node.id));
  assert.match(dot, /fillcolor="#fcf4d9"/);
  assert.doesNotMatch(dot, /fillcolor="#9ed9b4"/);
  assert.equal(nodeGraphStatus({ status: "open", completion: { source_reported_complete: true } }), "open");
  assert.equal(nodeGraphStatus({ status: "open", completion: { historical_kernel_checked: true } }), "open");
});

test("statement-aligned nodes render purple and retain formalized precedence", () => {
  assert.equal(nodeGraphStatus({ status: "open", completion: { statement_aligned: true } }), "statement-aligned");
  assert.equal(nodeGraphStatus({ status: "formalized", completion: { statement_aligned: true } }), "formalized");
  assert.equal(nodeGraphStatus({ status: "open", labels: ["formally_proved"] }), "formalized");
  assert.equal(nodeGraphStatus({ status: "open", completion: { exact_claim_closed: true, current_kernel_checked: true } }), "candidate");
  assert.equal(nodeGraphStatus({ status: "open", completion: { exact_claim_closed: true, current_kernel_checked: false } }), "open");
  const dot = formalizationDot(buildFormalizationGraph({ nodes: [{ id: "aligned", completion: { statement_aligned: true } }] }, "aligned"));
  assert.match(dot, /color="#7b3f98"/);
  assert.match(dot, /fillcolor="#eadcf2"/);
  const closedDot = formalizationDot(buildFormalizationGraph({ nodes: [{ id: "closed", status: "formalized", completion: { statement_aligned: true } }] }, "closed"));
  assert.match(closedDot, /color="#13663e"/);
  assert.match(closedDot, /fillcolor="#9ed9b4"/);
});

test("DAG fill follows the highest progress label, with formally proved green", () => {
  const color = (labels: string[], extra: Record<string, unknown> = {}) => {
    const model = buildFormalizationGraph({ nodes: [{ id: "n", labels, ...extra }] }, "n");
    return formalizationDot(model);
  };
  assert.equal(nodeGraphStatus({ labels: ["informal_stated"] }), "informal_stated");
  assert.equal(nodeGraphStatus({ labels: ["informal_stated", "proof_sketch"] }), "proof_sketch");
  assert.equal(nodeGraphStatus({ labels: ["informal_proved", "proof_sketch"] }), "informal_proved");
  assert.equal(nodeGraphStatus({ labels: ["formally_stated", "informal_proved"] }), "formally_stated");
  assert.equal(nodeGraphStatus({ labels: ["formally_stated", "formally_proved"] }), "formalized");
  assert.equal(nodeGraphStatus({ status: "formalized", labels: ["formally_proved", "conditionally_proved"] }), "conditionally_proved");
  assert.match(color(["informal_stated"]), /fillcolor="#dce8f0"/);
  assert.match(color(["proof_sketch"]), /fillcolor="#f6ebcf"/);
  assert.match(color(["informal_proved"]), /fillcolor="#cfeae4"/);
  assert.match(color(["formally_stated"]), /fillcolor="#ddd6f5"/);
  assert.match(color(["formally_proved"]), /fillcolor="#9ed9b4"/);
  assert.match(color(["formally_proved", "conditionally_proved"]), /fillcolor="#f9e0c9"/);
  assert.match(color(["informal_stated"], { completion: { current_kernel_checked: true } }), /fillcolor="#dce8f0"/);
  assert.match(color(["formally_stated"], { stale: true }), /fillcolor="#fbeddc"/);
});


test("stale, archived and failed states keep precedence over a recorded check", () => {
  const completion = { current_kernel_checked: true, source_reported_complete: true };
  assert.equal(nodeGraphStatus({ status: "formalized", stale: true, completion }), "stale");
  assert.equal(nodeGraphStatus({ status: "formalized", lifecycle: "archived", completion }), "archived");
  assert.equal(nodeGraphStatus({ status: "failed", completion }), "failed");
});

test("fit handles huge graphs without minimum zoom clipping and cursor zoom preserves anchors", () => {
  const fit = fitGraphTransform(500, 400, 1000000, 800000);
  assert.ok(fit.scale < 0.001);
  assert.ok(1000000 * fit.scale <= 464);
  assert.ok(800000 * fit.scale <= 364);
  const before = { x: 40, y: -25, scale: 0.7 };
  const after = zoomGraphAt(before, 2, 150, 90);
  assert.equal((150 - before.x) / before.scale, (150 - after.x) / after.scale);
  assert.equal((90 - before.y) / before.scale, (90 - after.y) / after.scale);
  assert.deepEqual(fitGraphTransform(0, 0, 0, 0), { x: 0, y: 0, scale: 1 });
});
