import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { buildChapterGraph, chapterDot, CHAPTER_NODE_RE } from '../dagGraph';
import { cachedLayout, layoutDot, prefetchLayouts } from '../vizInstance';

const SCALE_MIN = 0.05;
const SCALE_MAX = 6;

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
  const model = useMemo(() => buildChapterGraph(nodes, edges), [nodes, edges]);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [detail, setDetail] = useState(2);
  const [svg, setSvg] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const canvasRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<(HTMLDivElement & { __dagSvg?: string }) | null>(null);
  const transformRef = useRef({ x: 0, y: 0, scale: 1 });
  const rafRef = useRef(0);
  const dragRef = useRef<{ id: number; x: number; y: number; moved: number } | null>(null);
  const suppressClick = useRef(false);

  const dot = useMemo(() => chapterDot(model, expanded, detail), [model, expanded, detail]);

  useEffect(() => {
    if (!selected || !model.byId.has(selected)) return;
    const node = model.byId.get(selected)!;
    setExpanded(node.chapter);
    setDetail((current) => Math.max(current, node.level));
  }, [selected, model]);
  useEffect(() => {
    if (expanded != null && !model.chapters[expanded]) setExpanded(null);
  }, [expanded, model.chapters]);

  useEffect(() => {
    let cancelled = false;
    if (!model.nodes.length) {
      setSvg(null);
      return;
    }
    const hit = cachedLayout(dot);
    if (hit) {
      setSvg(hit);
      setError('');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError('');
    layoutDot(dot)
      .then((value) => { if (!cancelled) setSvg(value); })
      .catch((reason) => { if (!cancelled) setError(String(reason)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [dot, model.nodes.length]);

  useEffect(() => {
    if (!model.nodes.length) return;
    prefetchLayouts(model.chapters.map((chapter) => chapterDot(model, chapter.key, detail)));
  }, [model, detail]);

  const applyTransform = () => {
    rafRef.current = 0;
    if (!innerRef.current) return;
    const t = transformRef.current;
    innerRef.current.style.transform = `translate(calc(-50% + ${t.x}px), calc(-50% + ${t.y}px)) scale(${t.scale})`;
  };
  const scheduleTransform = () => {
    if (!rafRef.current) rafRef.current = requestAnimationFrame(applyTransform);
  };
  useEffect(() => () => cancelAnimationFrame(rafRef.current), []);

  const fit = () => {
    const canvas = canvasRef.current;
    const inner = innerRef.current;
    if (!canvas || !inner) return;
    const scale = Math.max(
      SCALE_MIN,
      Math.min(1, canvas.clientWidth / Math.max(inner.offsetWidth, 1), canvas.clientHeight / Math.max(inner.offsetHeight, 1)) * 0.94,
    );
    transformRef.current = { x: 0, y: 0, scale };
    scheduleTransform();
  };

  useLayoutEffect(() => {
    const host = innerRef.current;
    if (!host || svg == null || host.__dagSvg === svg) return;
    host.innerHTML = svg;
    host.__dagSvg = svg;
    const svgEl = host.querySelector('svg');
    if (!svgEl) return;
    svgEl.querySelectorAll('g.node').forEach((group) => {
      const el = group as SVGGElement;
      const title = el.querySelector('title');
      el.dataset.nodeId = (title?.textContent || '').trim();
      title?.remove();
    });
    svgEl.querySelectorAll('g.cluster').forEach((group) => {
      const el = group as SVGGElement;
      const title = el.querySelector('title');
      const match = /^cluster_(\d+)$/.exec((title?.textContent || '').trim());
      if (match) el.dataset.chapter = match[1];
      title?.remove();
    });
    svgEl.querySelectorAll('g.edge').forEach((group) => {
      const el = group as SVGGElement;
      const title = el.querySelector('title');
      const endpoints = (title?.textContent || '').split('->', 2).map((part) => part.trim());
      if (endpoints.length === 2) {
        el.dataset.source = endpoints[0];
        el.dataset.target = endpoints[1];
      }
      title?.remove();
    });
    requestAnimationFrame(fit);
  }, [svg]);

  useEffect(() => {
    const svgEl = innerRef.current?.querySelector('svg');
    if (!svgEl) return;

    const dependencies = new Set<string>();
    const dependents = new Set<string>();
    svgEl.querySelectorAll<SVGGElement>('g.edge').forEach((el) => {
      const source = el.dataset.source || '';
      const target = el.dataset.target || '';
      const incoming = !!selected && target === selected;
      const outgoing = !!selected && source === selected;
      if (incoming) dependencies.add(source);
      if (outgoing) dependents.add(target);
      el.classList.toggle('dag-edge-incoming', incoming);
      el.classList.toggle('dag-edge-outgoing', outgoing);
      el.style.opacity = selected && !incoming && !outgoing ? '0.07' : '1';
    });

    svgEl.querySelectorAll<SVGGElement>('g.node').forEach((el) => {
      const id = el.dataset.nodeId || '';
      const chapterMatch = CHAPTER_NODE_RE.exec(id);
      const passes = chapterMatch
        ? model.chapters[Number(chapterMatch[1])]?.nodeIds.some((nodeId) => !highlight || highlight.has(nodeId))
        : !highlight || highlight.has(id);
      const isSelected = !!selected && id === selected;
      const isDependency = dependencies.has(id);
      const isDependent = dependents.has(id);
      el.classList.toggle('dag-node-selected', isSelected);
      el.classList.toggle('dag-node-dependency', isDependency);
      el.classList.toggle('dag-node-dependent', isDependent);
      el.style.opacity = selected
        ? (isSelected || isDependency || isDependent ? '1' : '0.12')
        : (passes ? '1' : '0.12');
    });
  }, [svg, highlight, selected, model]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? canvas.clientHeight : 1;
      const t = transformRef.current;
      if (event.ctrlKey || event.metaKey) {
        const rect = canvas.getBoundingClientRect();
        const scale = Math.min(SCALE_MAX, Math.max(SCALE_MIN, t.scale * Math.exp(-event.deltaY * unit * 0.0022)));
        const ratio = scale / t.scale;
        const cx = rect.left + rect.width / 2 + t.x;
        const cy = rect.top + rect.height / 2 + t.y;
        transformRef.current = {
          x: t.x + (event.clientX - cx) * (1 - ratio),
          y: t.y + (event.clientY - cy) * (1 - ratio),
          scale,
        };
      } else {
        transformRef.current = { ...t, x: t.x - event.deltaX * unit, y: t.y - event.deltaY * unit };
      }
      scheduleTransform();
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [svg]);

  const onPointerDown = (event: React.PointerEvent) => {
    if (event.button !== 0) return;
    suppressClick.current = false;
    dragRef.current = { id: event.pointerId, x: event.clientX, y: event.clientY, moved: 0 };
  };
  const onPointerMove = (event: React.PointerEvent) => {
    const drag = dragRef.current;
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    drag.x = event.clientX;
    drag.y = event.clientY;
    drag.moved += Math.abs(dx) + Math.abs(dy);
    if (drag.moved > 3) canvasRef.current?.setPointerCapture(event.pointerId);
    const t = transformRef.current;
    transformRef.current = { ...t, x: t.x + dx, y: t.y + dy };
    scheduleTransform();
  };
  const onPointerEnd = (event: React.PointerEvent) => {
    if (dragRef.current?.id === event.pointerId) {
      suppressClick.current = dragRef.current.moved > 6;
      dragRef.current = null;
    }
  };
  const onClick = (event: React.MouseEvent) => {
    if (suppressClick.current) {
      suppressClick.current = false;
      return;
    }
    const target = event.target as Element;
    const node = target.closest('g.node') as SVGGElement | null;
    if (node) {
      const id = node.dataset.nodeId || '';
      const chapter = CHAPTER_NODE_RE.exec(id);
      if (chapter) {
        onSelectNode?.(null);
        setExpanded(Number(chapter[1]));
      }
      else if (model.byId.has(id)) onSelectNode?.(id);
      return;
    }
    const cluster = target.closest('g.cluster') as SVGGElement | null;
    if (cluster?.dataset.chapter != null) {
      onSelectNode?.(null);
      setExpanded(null);
    }
    else onSelectNode?.(null);
  };

  return (
    <div className="chapter-graph">
      <div className="chapter-graph-controls">
        <select
          value={expanded ?? ''}
          onChange={(event) => {
            onSelectNode?.(null);
            setExpanded(event.target.value === '' ? null : Number(event.target.value));
          }}
        >
          <option value="">All chapters</option>
          {model.chapters.map((chapter) => <option key={chapter.key} value={chapter.key}>{chapter.label}</option>)}
        </select>
        <select value={detail} onChange={(event) => setDetail(Number(event.target.value))}>
          <option value={2}>All detail</option>
          <option value={1}>Coarse + medium</option>
          <option value={0}>Coarse only</option>
        </select>
        <button type="button" onClick={fit}>Fit</button>
        <span>{expanded == null ? 'Select a chapter to expand it' : 'Select a node for details; select the chapter background to collapse it'}</span>
      </div>
      <div
        ref={canvasRef}
        className="chapter-graph-canvas"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        onClick={onClick}
      >
        <div ref={innerRef} className="chapter-graph-inner" />
        {loading && <div className="chapter-graph-loading">Laying out…</div>}
        {error && <div className="chapter-graph-error">Graphviz layout failed: {error}</div>}
        {!model.nodes.length && <div className="chapter-graph-empty">No dependency graph is available.</div>}
      </div>
    </div>
  );
}
