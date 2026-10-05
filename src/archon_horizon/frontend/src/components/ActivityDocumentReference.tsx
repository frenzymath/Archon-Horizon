import { lazy, Suspense, useEffect, useId, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUpRight, CircleDot, ListTodo, Network } from "lucide-react";
import { request } from "../pipeline/api";
import MathTitle from "./MathTitle";
import "./activity.css";

const DocumentView = lazy(() => import("./DocumentView"));

function nodeRecord(value: Record<string, any> | null, id: string): Record<string, any> | null {
  const nodes = Array.isArray(value?.nodes) ? value.nodes : [];
  return nodes.find((node: Record<string, any>) => node?.id === id || node?.label === id || node?.metadata?.label === id) || nodes[0] || null;
}

export default function ActivityDocumentReference({ projectId, id, kind, title: suppliedTitle }: {
  projectId: string; id: string; kind: "mission" | "objective" | "node"; title?: string;
}) {
  const [record, setRecord] = useState<Record<string, any> | null>(null);
  const [error, setError] = useState(false);
  const [preview, setPreview] = useState<{ left: number; top: number } | null>(null);
  const tooltip = useId();
  const Icon = kind === "mission" ? ListTodo : kind === "objective" ? Network : CircleDot;
  useEffect(() => {
    if (!preview || record) return;
    const controller = new AbortController();
    setError(false);
    const path = kind === "node"
      ? `/projects/${encodeURIComponent(projectId)}/dashboard/nodes/${encodeURIComponent(id)}`
      : kind === "objective"
        ? `/projects/${encodeURIComponent(projectId)}/dashboard/objectives/${encodeURIComponent(id)}`
        : `/missions/${encodeURIComponent(id)}`;
    request<Record<string, any>>(path, { signal: controller.signal })
      .then(value => {
        if (controller.signal.aborted) return;
        const next = kind === "node" ? nodeRecord(value, id) : value;
        if (next) setRecord(next);
        else setError(true);
      })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [projectId, id, kind, !!preview, record]);
  const show = (element: HTMLElement) => {
    const rect = element.getBoundingClientRect();
    setPreview({ left: Math.max(12, Math.min(rect.left, innerWidth - 372)), top: Math.max(12, Math.min(rect.bottom + 8, innerHeight - 320)) });
  };
  const href = kind === "node"
    ? `?project=${encodeURIComponent(projectId)}&node=${encodeURIComponent(id)}`
    : `?project=${encodeURIComponent(projectId)}&project_view=${kind === "mission" ? "missions" : "roadmap"}&${kind}=${encodeURIComponent(id)}`;
  const title = record?.title || suppliedTitle || id;
  const documentText = String(record?.content || record?.document || record?.markdown || record?.objective || record?.description || "").slice(0, 2400);
  return <>
    <a className="activity-document-reference" href={href} aria-describedby={preview ? tooltip : undefined}
      onMouseEnter={event => show(event.currentTarget)} onMouseLeave={() => setPreview(null)}
      onFocus={event => show(event.currentTarget)} onBlur={() => setPreview(null)}>
      <Icon size={12} /><span className="activity-reference-kind">{kind}</span><span className="activity-reference-title"><MathTitle title={title} /></span><ArrowUpRight size={11} />
    </a>
    {preview && createPortal(<div role="tooltip" id={tooltip} className="platform-node-preview activity-document-preview" style={preview}>
      <span><Icon size={13} />{kind}{record?.status ? ` / ${record.status}` : ""}</span>
      <strong><MathTitle title={title} /></strong>
      {record ? <Suspense fallback={null}><DocumentView document={documentText} showMetadata={false} className="activity-document-excerpt" /></Suspense> : <p>{error ? "Details unavailable" : "Loading..."}</p>}
    </div>, document.body)}
  </>;
}
