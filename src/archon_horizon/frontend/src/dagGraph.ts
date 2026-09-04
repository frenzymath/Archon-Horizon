const DEF_KINDS = new Set(['definition', 'example', 'remark', 'notation', 'convention']);
// These environments can be labelled and rendered in the document, but they
// are not formalisation targets. Keep this filter here as a cache-safe guard:
// an older published DAG may still contain prose nodes until it is resynced.
export const NON_FORMALIZATION_KINDS = new Set([
  'remark', 'notation', 'convention', 'example', 'conjecture', 'claim', 'fact',
  'exercise', 'note', 'proof', 'proposition_',
]);
const LEVEL: Record<string, number> = { coarse: 0, medium: 1, fine: 2 };
const DONE = new Set(['lean_ok', 'mathlib_ok']);

const COLORS = {
  border: { formalized: '#2e7d32', ready: '#1565c0', blocked: '#b0bec5' },
  fill: { done: '#66bb6a', local: '#c8e6c9', incomplete: '#ffcc80', ready: '#bbdefb', notready: '#eef1f4' },
  chapterFill: '#ede9fe', chapterBorder: '#7c3aed', chapterText: '#3b0a91',
  expandedFill: '#f3effc', expandedText: '#5b21b6', nodeText: '#1c2024', edge: '#8a93a0',
};

interface GraphNode {
  id: string;
  raw: any;
  chapter: number;
  level: number;
  statement: keyof typeof COLORS.border;
  proof: keyof typeof COLORS.fill;
  closed: boolean;
}

interface Chapter {
  key: number;
  label: string;
  count: number;
  done: number;
  nodeIds: string[];
}

export interface ChapterGraph {
  nodes: GraphNode[];
  byId: Map<string, GraphNode>;
  edges: Array<[string, string]>;
  chapters: Chapter[];
}

const escapeDot = (value: string) => value.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
const plain = (value: string) => value
  .replace(/\$([^$]+)\$/g, '$1')
  .replace(/\\(?:text|mathrm|operatorname)\{([^{}]*)\}/g, '$1')
  .replace(/\\[A-Za-z]+/g, '')
  .replace(/[{}]/g, '')
  .replace(/\s+/g, ' ')
  .trim();
const dotLabel = (value: string) => {
  const words = plain(value).split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = '';
  for (const word of words) {
    const next = current ? `${current} ${word}` : word;
    if (next.length <= 22 || !current) current = next;
    else { lines.push(current); current = word; if (lines.length === 2) break; }
  }
  if (current && lines.length < 3) lines.push(current);
  return lines.map(escapeDot).join('\\n') || 'untitled';
};

export function buildChapterGraph(rawNodes: any[], rawEdges: any[]): ChapterGraph {
  const unique = new Map<string, any>();
  rawNodes.forEach((node) => {
    const kind = String(node?.type ?? node?.kind ?? '').toLowerCase();
    if (node?.id != null && !NON_FORMALIZATION_KINDS.has(kind) && !unique.has(String(node.id))) {
      unique.set(String(node.id), node);
    }
  });
  const ids = new Set(unique.keys());
  const edges: Array<[string, string]> = [];
  const edgeSeen = new Set<string>();
  rawEdges.forEach((edge) => {
    const source = String(edge.source ?? '');
    const target = String(edge.target ?? '');
    const key = `${source}\0${target}`;
    if (source !== target && ids.has(source) && ids.has(target) && !edgeSeen.has(key)) {
      edgeSeen.add(key);
      edges.push([source, target]); // dependency -> dependent (Horizon shape)
    }
  });

  const chapterKeys: string[] = [];
  const chapterIndex = new Map<string, number>();
  unique.forEach((node) => {
    const label = String(node.chapter || node.group || 'Ungrouped');
    if (!chapterIndex.has(label)) { chapterIndex.set(label, chapterKeys.length); chapterKeys.push(label); }
  });
  const deps = new Map<string, string[]>();
  ids.forEach((id) => deps.set(id, []));
  edges.forEach(([source, target]) => deps.get(target)!.push(source));
  const locallyDone = (node: any) => DONE.has(String(node.lean_status || '')) || !!node.proved || !!node.mathlib_ok;
  const closedMemo = new Map<string, boolean>();
  const closing = new Set<string>();
  const isClosed = (id: string): boolean => {
    if (closedMemo.has(id)) return closedMemo.get(id)!;
    if (closing.has(id)) return false;
    closing.add(id);
    const result = locallyDone(unique.get(id)) && (deps.get(id) || []).every(isClosed);
    closing.delete(id);
    closedMemo.set(id, result);
    return result;
  };
  ids.forEach(isClosed);

  const nodes: GraphNode[] = [];
  unique.forEach((raw, id) => {
    const chapter = chapterIndex.get(String(raw.chapter || raw.group || 'Ungrouped'))!;
    const prerequisites = deps.get(id) || [];
    const done = locallyDone(raw);
    const allLinked = prerequisites.every((dep) => String(unique.get(dep)?.lean_status || '') !== 'empty');
    const allDone = prerequisites.every((dep) => locallyDone(unique.get(dep)));
    const status = String(raw.lean_status || 'empty');
    nodes.push({
      id,
      raw,
      chapter,
      level: LEVEL[String(raw.level)] ?? 2,
      statement: status !== 'empty' || done ? 'formalized' : (!prerequisites.length || allLinked ? 'ready' : 'blocked'),
      proof: isClosed(id) ? 'done' : done ? 'local' : (status === 'sorry' || raw.has_sorry) ? 'incomplete' : (!prerequisites.length || allDone) ? 'ready' : 'notready',
      closed: isClosed(id),
    });
  });
  const chapters = chapterKeys.map((label, key) => {
    const members = nodes.filter((node) => node.chapter === key);
    return { key, label: plain(label) || 'Ungrouped', count: members.length, done: members.filter((node) => node.closed).length, nodeIds: members.map((node) => node.id) };
  });
  return { nodes, byId: new Map(nodes.map((node) => [node.id, node])), edges, chapters };
}

export const CHAPTER_NODE_RE = /^__chapter_(\d+)$/;
const chapterNode = (key: number) => `__chapter_${key}`;

/** Graphviz attrs tuned for short dependency edges inside one chapter. */
const DOT_GRAPH_ATTRS =
  'rankdir=TB;bgcolor="transparent";newrank=true;splines=true;overlap=false;' +
  'concentrate=false;nodesep=0.5;ranksep=0.7;ordering=out;';

/**
 * Build the chapter-collapsed Graphviz DOT.
 *
 * - Overview (`expanded === null`): one super-node per chapter, edges only
 *   between chapters (aggregated cross-chapter dependencies).
 * - Expanded chapter: only that chapter's nodes and intra-chapter edges.
 *   Collapsed chapters are not drawn as peer nodes — cross-chapter edges used
 *   to pull long curved routes across the canvas and wreck the layout.
 */
export function chapterDot(model: ChapterGraph, expanded: number | null, maxLevel: number): string {
  let dot = 'strict digraph "" {\n';
  dot += `${DOT_GRAPH_ATTRS}\n`;
  dot += 'node [shape=box,style="rounded,filled",fontname="Helvetica",fontsize=11,margin="0.11,0.05",penwidth=1.8];\n';
  dot += `edge [color="${COLORS.edge}",arrowhead=vee,arrowsize=0.8,penwidth=1];\n`;
  dot += 'graph [fontname="Helvetica",fontsize=13,labeljust="l"];\n';

  if (expanded != null) {
    const chapter = model.chapters.find((entry) => entry.key === expanded);
    if (!chapter) return `${dot}}\n`;
    const visible = new Set(
      model.nodes
        .filter((node) => node.chapter === expanded && node.level <= maxLevel)
        .map((node) => node.id),
    );
    dot += `subgraph cluster_${chapter.key} { label="${escapeDot(chapter.label)}  (select background to collapse)";style="rounded,filled";fillcolor="${COLORS.expandedFill}";color="${COLORS.chapterBorder}";penwidth=2.4;fontcolor="${COLORS.expandedText}";fontsize=12.5;\n`;
    model.nodes.forEach((node) => {
      if (!visible.has(node.id)) return;
      const definition = DEF_KINDS.has(String(node.raw.type || node.raw.kind || ''));
      dot += `"${escapeDot(node.id)}" [shape=${definition ? 'box' : 'ellipse'},style=${definition ? '"rounded,filled"' : '"filled"'},fillcolor="${COLORS.fill[node.proof]}",color="${COLORS.border[node.statement]}",fontcolor="${COLORS.nodeText}",label="${dotLabel(node.raw.title || node.id)}"];\n`;
    });
    dot += '}\n';
    const seen = new Set<string>();
    model.edges.forEach(([source, target]) => {
      // edges are dependency → dependent; keep only edges fully inside the chapter
      if (!visible.has(source) || !visible.has(target) || source === target) return;
      const key = `${source}\0${target}`;
      if (seen.has(key)) return;
      seen.add(key);
      dot += `"${escapeDot(source)}" -> "${escapeDot(target)}" [style=dashed];\n`;
    });
    return `${dot}}\n`;
  }

  // Collapsed overview: chapter super-nodes + chapter→chapter edges only.
  model.chapters.forEach((chapter) => {
    if (!chapter.count) return;
    const pct = Math.round((100 * chapter.done) / chapter.count);
    dot += `"${chapterNode(chapter.key)}" [label="${escapeDot(chapter.label)}\\n${chapter.count} statements · ${pct}%",fillcolor="${COLORS.chapterFill}",color="${COLORS.chapterBorder}",penwidth=2.6,fontcolor="${COLORS.chapterText}"];\n`;
  });
  const seen = new Set<string>();
  model.edges.forEach(([source, target]) => {
    const sourceNode = model.byId.get(source);
    const targetNode = model.byId.get(target);
    if (!sourceNode || !targetNode || sourceNode.chapter === targetNode.chapter) return;
    const from = chapterNode(sourceNode.chapter);
    const to = chapterNode(targetNode.chapter);
    const key = `${from}\0${to}`;
    if (seen.has(key)) return;
    seen.add(key);
    // Horizon edge shape is dependency → dependent, so chapter of the
    // dependency points at the chapter of the dependent.
    dot += `"${from}" -> "${to}" [style=dashed];\n`;
  });
  return `${dot}}\n`;
}
