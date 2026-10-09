import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ArrowUpRight, LocateFixed, Maximize, RotateCcw, Star, ZoomIn, ZoomOut } from "lucide-react";
import { cachedLayout, layoutDot } from "../vizInstance";
import { buildFormalizationGraph, fitGraphTransform, formalizationDot, nodeHasLabel, nodeLabels, progressLabelPriority, progressLabelTitles, zoomGraphAt } from "../formalizationGraph";
import type { FormalizationItem, GraphTransform } from "../formalizationGraph";
import { mathMacros } from "../utils/document";
import RecordDates from "./RecordDates";
import { NodeSessionTags } from "./SessionActivityTags";
import { mathTitleHtml, mathTitleParts } from "../utils/mathTitle";
import MathTitle from "./MathTitle";
import { NodeProgressLabels } from "./TagList";
import "./formalization-dag.css";

type Item = FormalizationItem;

const semanticLabel = (item: Item | null | undefined) => String(item?.label || item?.metadata?.label || item?.id || "");
export default function FormalizationDAG({ graph, focusId = "", onOpenNode, activeNodeIds = new Set<string>(), nodeActivityByNode, renderNodeDetails }: {
  graph: Item;
  focusId?: string;
  onOpenNode: (node: Item) => void;
  activeNodeIds?: ReadonlySet<string>;
  nodeActivityByNode?: ReadonlyMap<string, Item[]>;
  renderNodeDetails?: (node: Item) => ReactNode;
}) {
  const showDefinitions = true;
  const [selectedKey, setSelectedKey] = useState(focusId ? `node:${focusId}` : "");
  const [layout, setLayout] = useState<{ dot: string; svg: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [zoom, setZoom] = useState(100);
  const canvasRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const transform = useRef<GraphTransform>({ x: 0, y: 0, scale: 1 });
  const fitted = useRef<GraphTransform>({ x: 0, y: 0, scale: 1 });
  const frame = useRef(0);
  const dragging = useRef<{ id: number; x: number; y: number; distance: number } | null>(null);
  const suppressClick = useRef(false);
  const model = useMemo(() => buildFormalizationGraph(graph, focusId, { showDefinitions, showArchived: false }), [graph, focusId, showDefinitions]);
  const dot = useMemo(() => formalizationDot(model), [model]);
  const svg = cachedLayout(dot) ?? (layout?.dot === dot ? layout.svg : null);
  const vertexMapKey = useMemo(
    () => JSON.stringify(model.vertices.map((vertex) => [vertex.dotId, vertex.key, vertex.item.title, vertex.item.metadata?.math_macros])),
    [model.vertices],
  );
  const activeNodeKey = useMemo(() => [...activeNodeIds].sort().join("\u0000"), [activeNodeIds]);
  const selected = model.byKey.get(selectedKey) || model.byKey.get(model.focusKey);
  const displayedNode = selected?.item;
  const selectedActivities = selected ? nodeActivityByNode?.get(selected.item.id) ?? [] : [];
  const projectId = graph.project_id || graph.projectId || selected?.item.project_id || "";


  const applyTransform = () => {
    frame.current = 0;
    if (!innerRef.current) return;
    const value = transform.current;
    innerRef.current.style.transform = `translate(calc(-50% + ${value.x}px), calc(-50% + ${value.y}px)) scale(${value.scale})`;
    setZoom(Math.round(value.scale * 100));
  };
  const schedule = () => { if (!frame.current) frame.current = requestAnimationFrame(applyTransform); };
  const fit = () => {
    const canvas = canvasRef.current;
    const inner = innerRef.current;
    if (!canvas || !inner) return;
    fitted.current = fitGraphTransform(canvas.clientWidth, canvas.clientHeight, inner.offsetWidth, inner.offsetHeight);
    transform.current = fitted.current;
    schedule();
  };
  const changeZoom = (factor: number, anchorX = 0, anchorY = 0) => {
    transform.current = zoomGraphAt(transform.current, factor, anchorX, anchorY, Math.min(0.02, fitted.current.scale / 4));
    schedule();
  };
  const focusSelected = () => {
    const element = [...(innerRef.current?.querySelectorAll<SVGGElement>("g.node") || [])].find((item) => item.dataset.graphKey === selected?.key);
    const canvas = canvasRef.current;
    if (!element || !canvas) return;
    transform.current = { x: 0, y: 0, scale: Math.max(fitted.current.scale, 0.85) };
    applyTransform();
    const area = canvas.getBoundingClientRect(), node = element.getBoundingClientRect();
    transform.current.x = area.left + area.width / 2 - node.left - node.width / 2;
    transform.current.y = area.top + area.height / 2 - node.top - node.height / 2;
    schedule();
  };
  useEffect(() => { setSelectedKey(focusId ? `node:${focusId}` : ""); }, [focusId]);
  useEffect(() => () => cancelAnimationFrame(frame.current), []);
  useEffect(() => {
    let cancelled = false;
    setError("");
    if (!model.vertices.length) { setLayout(null); setLoading(false); return; }
    const cached = cachedLayout(dot);
    if (cached) { setLoading(false); return; }
    setLoading(false);
    const loadingTimer = window.setTimeout(() => { if (!cancelled) setLoading(true); }, 150);
    layoutDot(dot).then((value) => { if (!cancelled) setLayout({ dot, svg: value }); })
      .catch((failure) => { if (!cancelled) { setError(String(failure)); setLayout(null); } })
      .finally(() => { window.clearTimeout(loadingTimer); if (!cancelled) setLoading(false); });
    return () => { cancelled = true; window.clearTimeout(loadingTimer); };
  }, [dot, model.vertices.length]);

  useLayoutEffect(() => {
    const host = innerRef.current;
    if (!host) return;
    if (!svg) { host.replaceChildren(); return; }
    const document = new DOMParser().parseFromString(svg, "image/svg+xml");
    const svgElement = document.documentElement;
    const byDotId = new Map(model.vertices.map((vertex) => [vertex.dotId, vertex]));
    const upstream = new Set(model.edges.filter(([parent]) => parent === selected?.key).map(([, child]) => child));
    const downstream = new Set(model.edges.filter(([, child]) => child === selected?.key).map(([parent]) => parent));
    svgElement.querySelectorAll("g.node").forEach((element) => {
      const vertex = byDotId.get(element.querySelector("title")?.textContent || "");
      if (!vertex) return;
      element.setAttribute("data-graph-key", vertex.key);
      element.setAttribute("data-node-id", vertex.item.id);
      element.setAttribute("data-node-type", vertex.type);
      element.setAttribute("tabindex", "0");
      element.setAttribute("role", "button");
      const label = vertex.item.title || semanticLabel(vertex.item);
      const active = activeNodeIds.has(vertex.item.id);
      element.setAttribute("aria-label", `Node: ${label}${active ? " (active mission)" : ""}`);
      const title = element.querySelector("title");
      if (title) title.textContent = label;
      element.classList.toggle("formalization-dag-selected", vertex.key === selected?.key);
      element.classList.toggle("formalization-dag-active", active);
      element.classList.toggle("formalization-dag-upstream", upstream.has(vertex.key));
      element.classList.toggle("formalization-dag-downstream", downstream.has(vertex.key));
    });
    const byDot = new Map(model.vertices.map((vertex) => [vertex.dotId, vertex.key]));
    svgElement.querySelectorAll("g.edge").forEach((element) => {
      const [from, to] = (element.querySelector("title")?.textContent || "").split("->");
      const source = byDot.get(from), target = byDot.get(to);
      const direction = source && target && source === selected?.key ? "outgoing" : source && target && target === selected?.key ? "incoming" : "neutral";
      element.setAttribute("data-edge-direction", direction);
    });
    svgElement.setAttribute("role", "group");
    svgElement.setAttribute("aria-label", "Formalization dependency graph");
    host.replaceChildren(window.document.importNode(svgElement, true));
    fit();
    cancelAnimationFrame(frame.current);
    applyTransform();
  }, [svg, vertexMapKey]);

  useEffect(() => {
    const upstream = new Set(model.edges.filter(([parent]) => parent === selected?.key).map(([, child]) => child));
    const downstream = new Set(model.edges.filter(([, child]) => child === selected?.key).map(([parent]) => parent));
    innerRef.current?.querySelectorAll<SVGGElement>("g.node").forEach((element) => {
      const active = element.dataset.graphKey === selected?.key;
      const missionActive = activeNodeIds.has(element.dataset.nodeId || "");
      element.classList.toggle("formalization-dag-selected", active);
      element.classList.toggle("formalization-dag-active", missionActive);
      element.classList.toggle("formalization-dag-upstream", upstream.has(element.dataset.graphKey || ""));
      element.classList.toggle("formalization-dag-downstream", downstream.has(element.dataset.graphKey || ""));
      element.setAttribute("aria-pressed", String(active));
      const label = element.querySelector("title")?.textContent || element.dataset.nodeId || "";
      element.setAttribute("aria-label", `Node: ${label}${missionActive ? " (active mission)" : ""}`);
    });
    const byDot = new Map(model.vertices.map((vertex) => [vertex.dotId, vertex.key]));
    innerRef.current?.querySelectorAll<SVGGElement>("g.edge").forEach((element) => {
      const [from, to] = (element.querySelector("title")?.textContent || "").split("->");
      const source = byDot.get(from), target = byDot.get(to);
      element.setAttribute("data-edge-direction", source === selected?.key ? "outgoing" : target === selected?.key ? "incoming" : "neutral");
    });
    innerRef.current?.querySelectorAll("foreignObject.formalization-dag-math").forEach((element) => element.remove());
    const selectedElement = [...(innerRef.current?.querySelectorAll<SVGGElement>("g.node") || [])].find((element) => element.dataset.graphKey === selected?.key);
    const vertex = selectedElement ? model.byKey.get(selectedElement.dataset.graphKey || "") : undefined;
    const label = String(vertex?.item.title || semanticLabel(vertex?.item) || "");
    if (selectedElement && vertex && mathTitleParts(label).some((part) => part.math)) {
      const shape = selectedElement.querySelector<SVGGraphicsElement>("ellipse, polygon");
      if (shape) {
        const bounds = shape.getBBox();
        const width = bounds.width * 0.7, height = bounds.height * 0.7;
        const foreign = window.document.createElementNS("http://www.w3.org/2000/svg", "foreignObject");
        foreign.setAttribute("class", "formalization-dag-math");
        foreign.setAttribute("x", String(bounds.x + (bounds.width - width) / 2));
        foreign.setAttribute("y", String(bounds.y + (bounds.height - height) / 2));
        foreign.setAttribute("width", String(width));
        foreign.setAttribute("height", String(height));
        const container = window.document.createElementNS("http://www.w3.org/1999/xhtml", "div");
        container.setAttribute("class", "formalization-dag-math-label");
        const content = window.document.createElementNS("http://www.w3.org/1999/xhtml", "span");
        content.setAttribute("class", "platform-math-title");
        content.innerHTML = mathTitleHtml(label, mathMacros(vertex.item.metadata || {}));
        const identifier = window.document.createElementNS("http://www.w3.org/1999/xhtml", "code");
        identifier.className = "platform-node-label";
        identifier.textContent = semanticLabel(vertex.item);
        content.append(identifier);
        container.append(content);
        foreign.append(container);
        selectedElement.append(foreign);
        const scale = Math.min(1, width / Math.max(1, content.scrollWidth), height / Math.max(1, content.scrollHeight));
        content.style.transform = `scale(${scale})`;
      }
    }
  }, [svg, selected?.key, vertexMapKey, activeNodeKey]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const observer = new ResizeObserver(() => {
      const inner = innerRef.current;
      if (!inner) return;
      // Update fit bounds without resetting the view as node details change page size.
      fitted.current = fitGraphTransform(canvas.clientWidth, canvas.clientHeight, inner.offsetWidth, inner.offsetHeight);
    });
    observer.observe(canvas);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = canvas.getBoundingClientRect();
      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1;
      if (event.ctrlKey || event.metaKey || event.deltaMode !== 0) {
        changeZoom(Math.exp(-event.deltaY * unit * 0.0022), event.clientX - rect.left - rect.width / 2, event.clientY - rect.top - rect.height / 2);
      } else {
        transform.current = { ...transform.current, x: transform.current.x - event.deltaX, y: transform.current.y - event.deltaY };
        schedule();
      }
    };
    canvas.addEventListener("wheel", wheel, { passive: false });
    return () => canvas.removeEventListener("wheel", wheel);
  }, []);

  const selectFromElement = (target: EventTarget) => {
    if (!(target instanceof Element)) return;
    const key = target.closest<SVGGElement>("g.node")?.dataset.graphKey;
    if (key && model.byKey.has(key)) setSelectedKey(key);
  };
  const selectNode = (node: Item) => {
    const key = `node:${node.id}`;
    if (model.byKey.has(key)) setSelectedKey(key);
    else onOpenNode(node);
  };
  const graphNodeCount = model.vertices.length;
  const formallyProvedNodeCount = useMemo(
    () => model.vertices.reduce((count, vertex) => count + (nodeLabels(vertex.item).includes("formally_proved") ? 1 : 0), 0),
    [model.vertices],
  );
  const checkedNodeCount = useMemo(
    () => model.vertices.filter((vertex) => vertex.item.completion?.current_kernel_checked).length,
    [model.vertices],
  );
  const statementAlignedNodeCount = useMemo(
    () => model.vertices.filter((vertex) => vertex.item.completion?.statement_aligned).length,
    [model.vertices],
  );
  const sourceCompleteNodeCount = useMemo(
    () => model.vertices.filter((vertex) => vertex.item.completion?.source_reported_complete).length,
    [model.vertices],
  );
  return <section className="formalization-dag" aria-label="Node graph workspace">
    <div className="formalization-dag-workspace">
      <div className="formalization-dag-graph">
        <div className="formalization-dag-toolbar">
      <div className="formalization-dag-tools">
        <span className="formalization-dag-count">
          <span>{graphNodeCount} nodes / {formallyProvedNodeCount} formally proved</span>
          {checkedNodeCount > 0 && <span title="Nodes with a recorded kernel pass for their current graph revision; exact-statement review and dependencies may remain open">{checkedNodeCount} kernel checked</span>}
          {statementAlignedNodeCount > 0 && <span title="Nodes whose current statement passed the independent translation and semantic comparison review">{statementAlignedNodeCount} Statement ok</span>}
          {sourceCompleteNodeCount > 0 && <span title="Completion reported by the imported source; independent of Horizon's exact-statement closure">{sourceCompleteNodeCount} source complete</span>}
        </span>
        <button type="button" aria-label="Zoom out" title="Zoom out" onClick={() => changeZoom(1 / 1.25)}><ZoomOut size={16} /></button>
        <output aria-label="Graph zoom">{zoom === 0 ? "<1" : zoom}%</output>
        <button type="button" aria-label="Zoom in" title="Zoom in" onClick={() => changeZoom(1.25)}><ZoomIn size={16} /></button>
        <button type="button" aria-label="Fit graph" title="Fit graph" onClick={fit}><Maximize size={16} /></button>
        <button type="button" aria-label="Zoom to selected node" title="Zoom to selected node" disabled={!selected} onClick={focusSelected}><LocateFixed size={16} /></button>
      <button type="button" aria-label="Reset graph view" title="Reset graph view" onClick={() => { setSelectedKey(model.focusKey); fit(); }}><RotateCcw size={16} /></button>
      </div>
        </div>
        <div className="formalization-dag-legend" aria-label="Graph legend"><span>Progress:</span>{progressLabelPriority.map((label) => <span key={label}><i className={label === "formally_proved" ? "formalized" : label} /> {progressLabelTitles[label]}</span>)}<span><Star className="formalization-dag-milestone-symbol" size={16} aria-hidden="true" /> Milestone</span><span><i className="definition" /> Definition</span><span><i className="active" /> Active mission</span><span><i className="incoming" /> Prerequisite</span><span><i className="outgoing" /> Dependent</span><span className="formalization-dag-direction-note">Arrows point from prerequisite to dependent</span>{model.hasCycle && <span className="formalization-dag-cycle" role="status">Dependency cycle</span>}</div>
        <div className="formalization-dag-canvas" ref={canvasRef}
          onPointerDown={(event) => {
            if (event.button !== 0) return;
            suppressClick.current = false;
            dragging.current = { id: event.pointerId, x: event.clientX, y: event.clientY, distance: 0 };
          }}
          onPointerMove={(event) => {
            const drag = dragging.current;
            if (!drag || drag.id !== event.pointerId) return;
            const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
            drag.x = event.clientX; drag.y = event.clientY; drag.distance += Math.abs(dx) + Math.abs(dy);
            if (drag.distance > 3) event.currentTarget.setPointerCapture(event.pointerId);
            transform.current = { ...transform.current, x: transform.current.x + dx, y: transform.current.y + dy };
            schedule();
          }}
          onPointerUp={(event) => {
            if (dragging.current?.id !== event.pointerId) return;
            suppressClick.current = dragging.current.distance > 6;
            dragging.current = null;
            if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
          }}
          onPointerCancel={() => { dragging.current = null; suppressClick.current = true; }}
          onClick={(event) => { if (suppressClick.current) { suppressClick.current = false; return; } selectFromElement(event.target); }}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") { event.preventDefault(); selectFromElement(event.target); }
          }}>
          <div className="formalization-dag-inner" ref={innerRef} />
          {loading && !svg && <div className="formalization-dag-message" role="status">Loading graph...</div>}
          {error && <div className="formalization-dag-message error" role="alert">Graph layout failed: {error}</div>}
          {!loading && !error && !model.vertices.length && <div className="formalization-dag-message">No nodes in this graph</div>}
        </div>
      </div>
      <aside className="formalization-dag-detail" aria-label="Selected graph node">
        {selected && <>
          <div className="formalization-dag-detail-head"><div><span>{displayedNode?.type || (displayedNode?.kind === "claim" ? "theorem" : displayedNode?.kind) || "Node"}</span><h2><MathTitle title={displayedNode?.title || semanticLabel(selected.item)} metadata={displayedNode?.metadata} /></h2></div>{selected.missing && <span className="formalization-dag-status failed">Unresolved</span>}</div>
          <code className="platform-node-label" aria-label="Node label">{semanticLabel(selected.item)}</code>
          <div className="formalization-dag-repo-badges">
            <NodeProgressLabels labels={nodeLabels(displayedNode || selected.item)} />
          </div>
          <RecordDates item={displayedNode || {}} />
          <div className="formalization-dag-sessions"><NodeSessionTags node={displayedNode} activities={selectedActivities} projectId={projectId} /></div>
          {(displayedNode?.completion?.current_kernel_checked || displayedNode?.completion?.source_reported_complete) && <div className="formalization-dag-evidence">
            {displayedNode?.completion?.current_kernel_checked && <span>Kernel check recorded</span>}
            {displayedNode?.completion?.source_reported_complete && <span>Source reports complete</span>}
            {!nodeHasLabel(displayedNode || selected.item, "formally_proved") && <span>Node is not formally proved</span>}
          </div>}
          {displayedNode && renderNodeDetails?.(displayedNode)}
          {!selected.missing && <button className="formalization-dag-open-node" type="button" onClick={() => onOpenNode(displayedNode || selected.item)}><ArrowUpRight size={15} /> Open node</button>}
        </>}
      </aside>
    </div>
  </section>;
}
