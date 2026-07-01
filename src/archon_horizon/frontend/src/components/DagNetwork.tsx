import React, { useEffect, useRef } from 'react';
// Separate imports (the pattern Archon's DAG uses): Network from vis-network,
// DataSet from vis-data. The `standalone` bundle breaks under our Vite build.
import { Network } from 'vis-network';
import { DataSet } from 'vis-data';

// Force-directed layout (forceAtlas2) — the same solver Archon uses. We run it
// LIVE (stabilization disabled) so you watch the graph settle instead of staring
// at a blank canvas, then freeze physics once it comes to rest.
const PHYSICS = {
  enabled: true,
  solver: 'forceAtlas2Based',
  forceAtlas2Based: {
    gravitationalConstant: -45,
    centralGravity: 0.012,
    springLength: 85,
    springConstant: 0.08,
    damping: 0.45,
    avoidOverlap: 0.7,
  },
  stabilization: false,
  minVelocity: 0.75,
  maxVelocity: 30,
} as const;

// Status palette mirrors Archon's dagColors: mathlib-ok blue, lean-ok green,
// not-ready red (the "no estimate / blocked" node, drawn larger), open yellow.
// Status is encoded by colour only — never as a text label on the node.
const GREEN = { background: '#dcfce7', border: '#22c55e' };
const BLUE = { background: '#dbeafe', border: '#3b82f6' };
const RED = { background: '#fee2e2', border: '#ef4444' };
const YELLOW = { background: '#fef9c3', border: '#eab308' };

const EDGE_BASE = '#cbd5e1';
const EDGE_OUT = '#2563eb'; // edges leaving the selected node (it depends on →)
const EDGE_IN = '#f59e0b'; // edges entering the selected node (← used by it)
const EDGE_DIM = '#e8edf3';

const ZOOM_MIN = 0.1;
const ZOOM_MAX = 3;

// Effort gradient (mirrors Archon's dagColors): yellow (small) → orange (large),
// green at 0, red at ∞ (null). Only used when leandag supplies effort numbers.
function effortColor(effort: number | null | undefined, maxEffort: number) {
  if (effort === null || effort === undefined) return RED;
  if (effort <= 0) return GREEN;
  const t = maxEffort > 0 ? Math.min(1, Math.sqrt(effort) / Math.sqrt(maxEffort)) : 0;
  const hue = 52 - (52 - 24) * t;
  return { background: `hsl(${hue}, 78%, 70%)`, border: `hsl(${hue}, 60%, 42%)` };
}

function hasEffort(n: any) {
  return Object.prototype.hasOwnProperty.call(n, 'effort_local');
}

function nodeColor(n: any, maxEffort: number) {
  if (n.mathlib_ok ?? n.mathlibok) return BLUE;
  if (hasEffort(n)) return effortColor(n.effort_local, maxEffort);
  if (n.proved ?? n.leanok) return GREEN;
  if (n.notready) return RED;
  return YELLOW;
}

// "∞" node: no effort estimate (leandag) or marked not-ready (parser) — drawn larger.
function isInfinity(n: any) {
  return hasEffort(n) ? n.effort_local === null || n.effort_local === undefined : !!n.notready;
}

export default function DagNetwork({
  nodes,
  edges,
  selected,
  onSelectNode,
  highlight,
}: {
  nodes: any[];
  edges: any[];
  selected?: string | null;
  onSelectNode?: (id: string | null) => void;
  highlight?: Set<string> | null;
}) {
  const container = useRef<HTMLDivElement>(null);
  const netRef = useRef<Network | null>(null);
  const nodesDSRef = useRef<DataSet<any> | null>(null);
  const edgesDSRef = useRef<DataSet<any> | null>(null);

  useEffect(() => {
    if (!container.current) return;
    // Dedupe by id: a duplicate \label{} yields two nodes with the same id, and
    // vis-network's DataSet THROWS on a duplicate id, blanking the whole page.
    const seenN = new Set<string>();
    const uniqueNodes = nodes.filter((n) => (seenN.has(n.id) ? false : (seenN.add(n.id), true)));
    const nodeIds = seenN;
    const seenE = new Set<string>();
    const uniqueEdges = edges.filter((e) => {
      if (!nodeIds.has(e.source) || !nodeIds.has(e.target)) return false;
      const k = `${e.source}->${e.target}`;
      return seenE.has(k) ? false : (seenE.add(k), true);
    });

    const maxEffort = uniqueNodes.reduce((m, n) => (typeof n.effort_local === 'number' ? Math.max(m, n.effort_local) : m), 0);
    const nodesDS = new DataSet<any>(
      uniqueNodes.map((n) => {
        const c = nodeColor(n, maxEffort);
        const lean = n.lean_name ?? n.lean;
        const done = (n.proved ?? n.leanok) || (n.mathlib_ok ?? n.mathlibok);
        return {
          id: n.id,
          label: String(n.id).split(':').pop() ?? n.id,
          title: [n.title || n.id, lean ? `lean: ${lean}` : '',
            (n.mathlib_ok ?? n.mathlibok) ? 'in mathlib' : done ? 'proved' : isInfinity(n) ? 'no estimate / blocked' : 'open',
          ].filter(Boolean).join('\n'),
          shape: 'dot',
          size: isInfinity(n) ? 16 : 12,
          color: { background: c.background, border: c.border, highlight: { background: c.background, border: '#0f172a' } },
          borderWidth: 2,
          font: { size: 11, color: '#334155', face: 'Inter, -apple-system, sans-serif' },
        };
      }),
    );
    const edgesDS = new DataSet<any>(
      uniqueEdges.map((e, i) => ({ id: i, from: e.source, to: e.target, color: { color: EDGE_BASE, highlight: EDGE_BASE } })),
    );
    nodesDSRef.current = nodesDS;
    edgesDSRef.current = edgesDS;

    const network = new Network(container.current, { nodes: nodesDS, edges: edgesDS } as any, {
      layout: { improvedLayout: uniqueNodes.length <= 250 },
      physics: PHYSICS as any,
      edges: {
        smooth: { enabled: true, type: 'cubicBezier', roundness: 0.4 },
        width: 1.2,
        arrows: { to: { enabled: true, scaleFactor: 0.9, type: 'arrow' } },
      },
      nodes: { shape: 'dot' },
      // Custom wheel handling below pans on swipe / zooms on pinch, so disable
      // vis-network's built-in (unintuitive) scroll-to-zoom.
      interaction: { hover: true, tooltipDelay: 120, zoomView: false, dragView: true },
    });
    netRef.current = network;

    // Fit once the first frame is drawn so the settling graph is actually in view,
    // and freeze physics once it comes to rest.
    network.once('afterDrawing', () => network.fit({ animation: false } as any));
    network.on('stabilized', () => network.setOptions({ physics: false }));

    if (onSelectNode) {
      network.on('selectNode', (p: any) => onSelectNode(p.nodes[0] ?? null));
      network.on('deselectNode', () => onSelectNode(null));
    }

    // Trackpad: two-finger swipe pans, pinch (ctrlKey) zooms — matches Archon.
    const onWheel = (e: WheelEvent) => {
      const net = netRef.current;
      if (!net) return;
      e.preventDefault();
      e.stopPropagation();
      const scale = net.getScale();
      const pos = net.getViewPosition();
      if (e.ctrlKey) {
        const f = e.deltaY > 0 ? 0.9 : 1 / 0.9;
        net.moveTo({ scale: Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, scale * f)), animation: false } as any);
      } else {
        net.moveTo({ position: { x: pos.x + e.deltaX / scale, y: pos.y + e.deltaY / scale }, animation: false } as any);
      }
    };
    const el = container.current;
    el.addEventListener('wheel', onWheel, { passive: false });

    const ro = new ResizeObserver(() => network.redraw());
    ro.observe(el);

    return () => {
      el.removeEventListener('wheel', onWheel);
      ro.disconnect();
      network.destroy();
      netRef.current = null;
      nodesDSRef.current = null;
      edgesDSRef.current = null;
    };
  }, [nodes, edges]);

  // Selection: colour outgoing vs incoming edges with two distinct colours.
  useEffect(() => {
    const net = netRef.current;
    const edgesDS = edgesDSRef.current;
    if (!net || !edgesDS) return;
    if (selected) {
      net.selectNodes([selected]);
      net.focus(selected, { scale: 1.1, animation: true });
      edgesDS.update(
        (edgesDS.get() as any[]).map((e) => {
          const out = e.from === selected;
          const inc = e.to === selected;
          const color = out ? EDGE_OUT : inc ? EDGE_IN : EDGE_DIM;
          return { id: e.id, color: { color, highlight: color }, width: out || inc ? 2.6 : 1 };
        }),
      );
    } else {
      net.unselectAll();
      edgesDS.update((edgesDS.get() as any[]).map((e) => ({ id: e.id, color: { color: EDGE_BASE, highlight: EDGE_BASE }, width: 1.2 })));
    }
  }, [selected]);

  // Filtering overlay: fade nodes that don't match the active filter/search.
  useEffect(() => {
    const nodesDS = nodesDSRef.current;
    if (!nodesDS) return;
    nodesDS.update(
      (nodesDS.get() as any[]).map((vn) => {
        const dim = highlight != null && !highlight.has(vn.id);
        return { id: vn.id, opacity: dim ? 0.18 : 1 };
      }),
    );
  }, [highlight]);

  return <div ref={container} style={{ width: '100%', height: '100%', minHeight: 400, border: '1px solid var(--border)', borderRadius: '8px', background: 'var(--bg-primary)' }} />;
}
