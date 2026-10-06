import { mathTitleParts } from "./utils/mathTitle";

export type FormalizationItem = Record<string, any>;
export type GraphVertex = { key: string; dotId: string; item: FormalizationItem; type: "node"; missing: boolean };
export type FormalizationModel = { vertices: GraphVertex[]; edges: Array<[string, string]>; byKey: Map<string, GraphVertex>; focusKey: string; hasCycle: boolean; hiddenCount: number };
export type GraphTransform = { x: number; y: number; scale: number };

const nodeKey = (id: string) => `node:${id}`;
const isArchived = (item: FormalizationItem) => item.lifecycle === "archived" || item.status === "archived" || item.route_archived === true;
const isDefinition = (item: FormalizationItem) => ["definition", "construction", "interface"].includes(item.kind || item.metadata?.type);
export const progressLabelTitles: Record<string, string> = {
  open: "Open",
  in_progress: "In Progress",
  stale: "Needs Reassessment",
  informal_stated: "Informal Stated",
  proof_sketch: "Proof Sketch",
  informal_proved: "Informal Proved",
  formally_stated: "Formally Stated",
  formally_proved: "Formally Proved",
  conditionally_proved: "Conditionally Proved",
  kernel_checked: "Kernel Checked",
};
/** Highest first: a node may carry several labels; DAG color uses one. */
export const progressLabelPriority = [
  "stale", "conditionally_proved", "formally_proved", "formally_stated", "informal_proved", "proof_sketch", "informal_stated", "kernel_checked", "in_progress", "open",
] as const;
export const graphStatusColors: Record<string, [string, string]> = {
  formalized: ["#13663e", "#9ed9b4"],
  conditionally_proved: ["#a55b24", "#f9e0c9"],
  formally_stated: ["#4b3f9a", "#ddd6f5"],
  informal_proved: ["#1d7a6c", "#cfeae4"],
  proof_sketch: ["#b8893a", "#f6ebcf"],
  informal_stated: ["#4e738c", "#dce8f0"],
  "statement-aligned": ["#7b3f98", "#eadcf2"],
  candidate: ["#bc913d", "#fcf4d9"],
  failed: ["#bd6767", "#f8e4e4"],
  archived: ["#9da6af", "#edf0f3"],
  stale: ["#c18b4b", "#fbeddc"],
  open: ["#2f789b", "#eaf3f8"],
  in_progress: ["#997000", "#fff1bd"],
};

const normalizeLabel = (value: unknown) => String(value || "").trim().toLowerCase().replace(/\s+/g, "_");

export function nodeLabels(item: FormalizationItem): string[] {
  const listed = item.labels ?? item.metadata?.labels ?? [];
  const labels = Array.isArray(listed) ? listed.map(normalizeLabel) : [];
  const stage = normalizeLabel(item.stage || item.metadata?.stage);
  if (stage) labels.push(stage);
  return [...new Set(labels)].filter((label) => label && label !== "potentially_outdated" && label !== "potentially-outdated");
}

function childIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return [...new Set(value.flatMap((child) => {
    const id = typeof child === "string" ? child : child?.claim_id || child?.node_id || child?.id;
    return typeof id === "string" && id ? [id] : [];
  }))];
}

export function buildFormalizationGraph(graph: FormalizationItem, focusId = "", options = { showDefinitions: true, showArchived: false }): FormalizationModel {
  const allNodes = new Map<string, FormalizationItem>();
  for (const node of graph.nodes ?? []) if (typeof node.id === "string" && !allNodes.has(node.id)) allNodes.set(node.id, node);
  const vertices = new Map<string, GraphVertex>();
  const dependencies = new Map<string, Set<string>>();
  const addVertex = (key: string, item: FormalizationItem, missing = false) => {
    if (!vertices.has(key)) vertices.set(key, { key, dotId: `v${vertices.size}`, item, type: "node", missing });
    if (!dependencies.has(key)) dependencies.set(key, new Set());
  };
  const focusKey = focusId ? nodeKey(focusId) : "";
  const pending = graph.scope === "objective" || graph.scope === "project" || !focusId
    ? [...allNodes.keys()] : allNodes.has(focusId) ? [focusId] : [];
  const visited = new Set<string>();
  for (let cursor = 0; cursor < pending.length; cursor++) {
    const id = pending[cursor];
    if (visited.has(id)) continue;
    visited.add(id);
    const item = allNodes.get(id);
    if (item && !options.showArchived && isArchived(item) && id !== focusId) continue;
    const key = nodeKey(id);
    addVertex(key, item || { id, title: id, status: "unresolved" }, !item);
    if (!item) continue;
    const attach = (parent: string, child: string) => {
      const target = allNodes.get(child);
      if (target && !options.showArchived && isArchived(target) && child !== focusId) return;
      dependencies.get(parent)!.add(nodeKey(child));
      pending.push(child);
    };
    for (const child of childIds(item.children ?? item.metadata?.children)) attach(key, child);
  }
  const visible = new Map([...vertices].filter(([key, vertex]) => key === focusKey || options.showDefinitions || !isDefinition(vertex.item)));
  const edges: Array<[string, string]> = [];
  // Bypass hidden definitions without losing the reachable claims beyond them.
  for (const key of visible.keys()) {
    const next = [...(dependencies.get(key) || [])];
    const seen = new Set<string>();
    for (let cursor = 0; cursor < next.length; cursor++) {
      const target = next[cursor];
      if (seen.has(target)) continue;
      seen.add(target);
      if (visible.has(target)) edges.push([key, target]);
      else next.push(...(dependencies.get(target) || []));
    }
  }
  const indegree = new Map([...vertices.keys()].map((key) => [key, 0]));
  for (const targets of dependencies.values()) for (const target of targets) indegree.set(target, (indegree.get(target) || 0) + 1);
  const roots = [...indegree].filter(([, count]) => count === 0).map(([key]) => key);
  for (let cursor = 0; cursor < roots.length; cursor++) {
    for (const target of dependencies.get(roots[cursor]) || []) {
      const remaining = (indegree.get(target) || 0) - 1;
      indegree.set(target, remaining);
      if (remaining === 0) roots.push(target);
    }
  }
  return { vertices: [...visible.values()], edges, byKey: visible, focusKey, hasCycle: roots.length !== vertices.size, hiddenCount: vertices.size - visible.size };
}

export type GraphStatus = keyof typeof graphStatusColors;

export function nodeProgressLabel(item: FormalizationItem): (typeof progressLabelPriority)[number] | undefined {
  const labels = nodeLabels(item);
  return progressLabelPriority.find((label) => labels.includes(label));
}

export function nodeGraphStatus(item: FormalizationItem): GraphStatus {
  if (isArchived(item)) return "archived";
  if (item.stale || item.status === "stale") return "stale";
  if (["failed", "rejected", "disproved"].includes(item.status)) return "failed";
  if (nodeHasLabel(item, "conditionally_proved")) return "conditionally_proved";
  if (item.status === "formalized" || nodeHasLabel(item, "formally_proved")) return "formalized";
  const progress = nodeProgressLabel(item);
  if (progress && progress !== "formally_proved") return progress;
  if (item.completion?.statement_aligned) return "statement-aligned";
  if (item.completion?.current_kernel_checked) return "candidate";
  return ["candidate", "conditional"].includes(item.status) ? "candidate" : "open";
}

export function graphStatusTitle(status: GraphStatus): string {
  if (status === "formalized") return progressLabelTitles.formally_proved;
  return progressLabelTitles[status] || status.replace(/_/g, " ");
}

export function nodeOpenPullCount(item: FormalizationItem): number {
  const listed = Number(item.open_pull_count);
  if (Number.isFinite(listed) && listed > 0) return listed;
  const pulls = Array.isArray(item.pulls) ? item.pulls : [];
  return pulls.filter((pull) => ["open", "waiting_review", "waiting_author"].includes(String(pull?.state || ""))).length;
}

export function nodeHasLabel(item: FormalizationItem, wanted: string): boolean {
  return nodeLabels(item).includes(normalizeLabel(wanted));
}

const dotQuote = (value: string) => `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"').replace(/[\r\n\t\u0000-\u001f\u007f]/g, " ")}"`;
function dotLabel(value: string): string {
  const chars = Array.from(value.replace(/\s+/g, " ").trim());
  const text = chars.length > 92 ? `${chars.slice(0, 89).join("")}...` : chars.join("");
  const lines: string[] = [];
  let line = "";
  for (const word of text.split(" ")) {
    if (line && Array.from(`${line} ${word}`).length > 28) { lines.push(line); line = ""; }
    const chunks = Array.from(word);
    while (chunks.length > 28) { lines.push(chunks.splice(0, 28).join("")); }
    if (chunks.length) line = line ? `${line} ${chunks.join("")}` : chunks.join("");
  }
  if (line) lines.push(line);
  return lines.map((part) => dotQuote(part).slice(1, -1)).join("\\n");
}

export function formalizationDot(model: FormalizationModel): string {
  const lines = ['digraph "Formalization" {', 'graph [rankdir=TB,bgcolor="transparent",pad=0.3,nodesep=0.4,ranksep=0.62,pack=true,packmode="array",splines=polyline,overlap=false,outputorder=edgesfirst];', 'node [fontname="Helvetica",fontsize=11,fontcolor="#243943",margin="0.14,0.09",style=filled,penwidth=1.4];', 'edge [color="#a4afb7",arrowsize=0.65,arrowhead=vee,penwidth=1.05];'];
  for (const vertex of model.vertices) {
    const [border, fill] = graphStatusColors[nodeGraphStatus(vertex.item)];
    const milestone = nodeHasLabel(vertex.item, "milestone");
    const title = vertex.item.title || vertex.item.label || vertex.item.metadata?.label || vertex.item.id;
    const math = mathTitleParts(String(title)).some((part) => part.math);
    const identifier = String(vertex.item.label || vertex.item.id);
    const pulls = nodeOpenPullCount(vertex.item);
    const progress = nodeLabels(vertex.item).filter((item) => item in progressLabelTitles)
      .map((item) => progressLabelTitles[item]).join(" · ");
    const extra = [progress, pulls ? `${pulls} PR${pulls === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ");
    const label = math ? 'label="",width=3.8,height=1.7,fixedsize=true' : `label="${dotLabel(String(title))}\\n${dotLabel(identifier)}${extra ? `\\n${dotLabel(extra)}` : ""}"`;
    lines.push(`${vertex.dotId} [shape=${isDefinition(vertex.item) ? "box" : "ellipse"},${label},color=${dotQuote(border)},fillcolor=${dotQuote(fill)},penwidth=${vertex.key === model.focusKey ? 2.5 : milestone ? 2.2 : 1.4},peripheries=${milestone ? 2 : 1},tooltip=${dotQuote(identifier)}];`);
  }
  // Lean blueprint direction: prerequisites above their dependent conclusions.
  for (const [parent, child] of model.edges) lines.push(`${model.byKey.get(child)!.dotId} -> ${model.byKey.get(parent)!.dotId};`);
  lines.push("}");
  return lines.join("\n");
}

export function fitGraphTransform(viewportWidth: number, viewportHeight: number, graphWidth: number, graphHeight: number): GraphTransform {
  const width = Math.max(1, Number.isFinite(graphWidth) ? graphWidth : 1);
  const height = Math.max(1, Number.isFinite(graphHeight) ? graphHeight : 1);
  const scale = Math.min(1, Math.max(1, viewportWidth - 36) / width, Math.max(1, viewportHeight - 36) / height);
  return { x: 0, y: 0, scale };
}

export function zoomGraphAt(current: GraphTransform, factor: number, anchorX: number, anchorY: number, minimum = 0.001): GraphTransform {
  const scale = Math.max(minimum, Math.min(8, current.scale * factor));
  const ratio = scale / current.scale;
  return { x: anchorX - (anchorX - current.x) * ratio, y: anchorY - (anchorY - current.y) * ratio, scale };
}
