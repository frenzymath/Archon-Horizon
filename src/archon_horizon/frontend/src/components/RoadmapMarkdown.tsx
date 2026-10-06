import { lazy, Suspense, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Check, Circle, CircleDot, GitBranch, LoaderCircle, OctagonPause } from "lucide-react";
import { blueprintDisplayMath } from "../utils/blueprintSyntax";
import { bibliographyAnchor, bibliographyEntriesFromMarkdown, bibliographyEntry, citationDetail, citationLabel, type BibliographyEntry } from "../utils/bibliography";
import { documentBody, mathMacros, splitDocument, type MathMacros } from "../utils/document";
import MarkdownPre from "./MarkdownPre";
import MathTitle from "./MathTitle";
import "katex/dist/katex.min.css";
import "./blueprint.css";

const PreviewMarkdown = lazy(() => import("./BlueprintMarkdown"));

type Item = Record<string, any>;
type MarkdownNode = {
  type: string;
  value?: string;
  checked?: boolean | null;
  depth?: number;
  url?: string;
  data?: Record<string, any>;
  children?: MarkdownNode[];
};

export function roadmapSyntax(options: { title?: string; checklist?: boolean } = {}) {
  return (tree: MarkdownNode) => {
    const heading = tree.children?.[0];
    if (options.title && heading?.type === "heading" && heading.depth === 1) {
      const headingText = heading.children?.map((item) => item.value || "").join("").replace(/^Objective\s*:\s*/i, "").trim();
      if (headingText === options.title.trim()) tree.children = tree.children!.slice(1);
    }
    const visit = (node: MarkdownNode) => {
      if (node.type === "code" || node.type === "inlineCode" || node.type === "link" || node.type === "image") return;
      if (node.type === "listItem" && options.checklist !== false) {
        let status = typeof node.checked === "boolean" ? (node.checked ? "done" : "pending") : "";
        const first = node.children?.[0]?.children?.[0];
        const marker = first?.type === "text" && first.value?.match(/^\[(blocked|in progress)\]\s+/i);
        if (marker && first) {
          status = marker[1].toLowerCase() === "blocked" ? "blocked" : "in_progress";
          first.value = first.value!.slice(marker[0].length);
        }
        if (status) {
          delete node.checked;
          node.data = { ...node.data, hProperties: { ...node.data?.hProperties, "data-roadmap-status": status } };
        }
      }
      if (!node.children) return;
      node.children = node.children.flatMap((child) => {
        if (child.type !== "text" || !child.value) { visit(child); return [child]; }
        const pieces: MarkdownNode[] = [];
        const pattern = /\[\[(node|ref):([A-Za-z0-9][A-Za-z0-9_.:-]*)(?:\|([^\]\r\n]+))?\]\]/g;
        let offset = 0;
        for (const match of child.value.matchAll(pattern)) {
          if (match.index! > offset) pieces.push({ type: "text", value: child.value.slice(offset, match.index) });
          if (match[1] === "node" && !match[3]) {
            pieces.push({ type: "link", url: `node:${match[2]}`, children: [{ type: "text", value: match[2] }] });
          } else if (match[1] === "ref") {
            const locator = match[3]?.trim();
            const url = `ref:${encodeURIComponent(match[2])}${locator ? `?locator=${encodeURIComponent(locator)}` : ""}`;
            pieces.push({ type: "link", url, children: [{ type: "text", value: match[2] }] });
          } else {
            pieces.push({ type: "text", value: match[0] });
          }
          offset = match.index! + match[0].length;
        }
        if (!pieces.length) return [child];
        if (offset < child.value.length) pieces.push({ type: "text", value: child.value.slice(offset) });
        return pieces;
      });
    };
    visit(tree);
  };
}

export function NodeReference({ id, children, nodes, projectId, onNode, nodeResolver, referenceLookupComplete = true }: {
  id: string; children: React.ReactNode; nodes: Item[]; projectId: string; onNode?: (node: Item) => void; nodeResolver?: (id: string) => Promise<Item | null>; referenceLookupComplete?: boolean;
}) {
  const summary = nodes.find((item) => item.id === id || item.label === id || item.metadata?.label === id);
  const [resolvedNode, setResolvedNode] = useState<Item | null>(null);
  const requested = useRef(false);
  const resolutionEpoch = useRef(0);
  const node = resolvedNode || summary;
  const tooltipId = useId();
  const [preview, setPreview] = useState<{ left: number; top: number } | null>(null);
  const [requestState, setRequestState] = useState<"idle" | "loading" | "missing" | "error">("idle");
  useEffect(() => { resolutionEpoch.current += 1; setResolvedNode(null); requested.current = false; setRequestState("idle"); }, [id, projectId]);
  const resolve = () => {
    if (!nodeResolver || requested.current || node?.document || node?.markdown) return;
    requested.current = true;
    setRequestState("loading");
    const epoch = resolutionEpoch.current;
    void nodeResolver(id).then((result) => {
      if (resolutionEpoch.current !== epoch) return;
      if (result) setResolvedNode(result);
      setRequestState(result ? "idle" : "missing");
    }).catch(() => {
      if (resolutionEpoch.current !== epoch) return;
      requested.current = false;
      setRequestState("error");
    });
  };
  const show = (element: HTMLElement) => {
    const rect = element.getBoundingClientRect();
    setPreview({ left: Math.max(12, Math.min(rect.left, window.innerWidth - 336)), top: rect.bottom + 8 });
    resolve();
  };
  const label = String(children) === id && node ? node.title : children;
  const target = node?.label || node?.metadata?.label || id;
  return <>
    <a
      className={`platform-node-reference ${node ? "" : referenceLookupComplete ? "unresolved" : "loading"}`}
      href={`?project=${encodeURIComponent(projectId)}&node=${encodeURIComponent(target)}`}
      title={node ? `${node.title} (${node.status || "open"})` : referenceLookupComplete ? `Unresolved node: ${id}` : `Loading node: ${id}`}
      aria-describedby={preview ? tooltipId : undefined}
      onMouseEnter={(event) => show(event.currentTarget)} onMouseLeave={() => setPreview(null)}
      onFocus={(event) => show(event.currentTarget)} onBlur={() => setPreview(null)}
      onClick={(event) => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        if (!onNode) { resolve(); return; }
        event.preventDefault();
        setPreview(null);
        onNode(node || { id, title: id });
      }}
    ><CircleDot size={12} />{typeof label === "string" ? <MathTitle title={label} metadata={node?.metadata} /> : label}</a>
    {preview && createPortal(<div className="platform-node-preview platform-node-math-preview" id={tooltipId} role="tooltip" style={{ left: preview.left, top: Math.max(12, Math.min(preview.top, window.innerHeight - 250)) }}>
      <span><GitBranch size={13} />{node?.status || (requestState === "loading" || !referenceLookupComplete ? "loading" : requestState === "error" ? "retry available" : "unresolved")}</span>
      <strong><MathTitle title={node?.title || id} metadata={node?.metadata} /></strong>
      {node ? <Suspense fallback={<p>{node.title}</p>}><NodePreview document={node.document || node.markdown || ""} /></Suspense> : <p>{requestState === "loading" || !referenceLookupComplete ? "Loading node details..." : requestState === "error" ? "Node details could not be loaded; hover again to retry." : "This node is not available in this project."}</p>}
      <small>{id}</small>
    </div>, document.body)}
  </>;
}

function NodePreview({ document }: { document: string }) {
  const parsed = splitDocument(document);
  const content = documentBody(parsed);
  return <PreviewMarkdown content={content.slice(0, 2000)} mathMacros={mathMacros(parsed.metadata)} bibliographyEntries={bibliographyEntriesFromMarkdown(content)} />;
}

function parseReferenceHref(href: string): { key: string; locator?: string } {
  const [encodedKey, query = ""] = href.slice(4).split("?", 2);
  const locator = new URLSearchParams(query).get("locator") || undefined;
  try { return { key: decodeURIComponent(encodedKey), locator }; }
  catch { return { key: encodedKey, locator }; }
}

export function ReferenceCitation({ href, entries }: { href: string; entries: BibliographyEntry[] }) {
  const { key, locator } = parseReferenceHref(href);
  const entry = bibliographyEntry(entries, key);
  if (!entry) return <span className="platform-reference-citation unresolved" title={`Undefined bibliography reference: ${key}`}>[missing: {key}{locator ? `, ${locator}` : ""}]</span>;
  const target = entry.url || `#${bibliographyAnchor(entry.key)}`;
  const external = Boolean(entry.url);
  return <a className="platform-reference-citation" href={target} title={citationDetail(entry, locator)} target={external ? "_blank" : undefined} rel={external ? "noopener noreferrer" : undefined}>
    [{citationLabel(entry, locator)}]
  </a>;
}

export default function RoadmapMarkdown({ content, nodes, projectId, onNode, title, bibliographyEntries = [], mathMacros: documentMathMacros = {}, nodeResolver, referenceLookupComplete }: {
  content: string; nodes: Item[]; projectId: string; onNode?: (node: Item) => void; title?: string; bibliographyEntries?: BibliographyEntry[]; mathMacros?: MathMacros; nodeResolver?: (id: string) => Promise<Item | null>; referenceLookupComplete?: boolean;
}) {
  const hasMath = useMemo(() => /\$|\\\[|\\\(/.test(content), [content]);
  return <div className="platform-roadmap-markdown">
    <ReactMarkdown skipHtml remarkPlugins={hasMath ? [remarkGfm, remarkMath, blueprintDisplayMath, [roadmapSyntax, { title }]] : [remarkGfm, [roadmapSyntax, { title }]]} rehypePlugins={hasMath ? [[rehypeKatex, { trust: false, throwOnError: false, strict: "ignore", macros: { ...documentMathMacros }, maxExpand: 1000 }]] : []} urlTransform={(url) => url.startsWith("node:") || url.startsWith("ref:") ? url : defaultUrlTransform(url)} components={{
      pre: MarkdownPre,
      a: ({ href, children }) => href?.startsWith("ref:")
        ? <ReferenceCitation href={href} entries={bibliographyEntries} />
        : href?.startsWith("node:") && (onNode || nodeResolver)
        ? <NodeReference id={href.slice(5)} nodes={nodes} projectId={projectId} onNode={onNode} nodeResolver={nodeResolver} referenceLookupComplete={referenceLookupComplete}>{children}</NodeReference>
        : <a href={href?.startsWith("node:") ? `?project=${encodeURIComponent(projectId)}&node=${encodeURIComponent(href.slice(5))}` : href} rel="noreferrer">{children}</a>,
      li: ({ node, children }) => {
        const status = String(node?.properties?.["data-roadmap-status"] || "");
        const Icon = status === "done" ? Check : status === "blocked" ? OctagonPause : status === "in_progress" ? LoaderCircle : Circle;
        return status ? <li className={`platform-roadmap-item ${status}`}><span className="platform-roadmap-item-icon" title={status.replace("_", " ")} aria-label={status.replace("_", " ")}><Icon size={16} /></span><div>{children}</div></li> : <li>{children}</li>;
      },
    }}>{content}</ReactMarkdown>
  </div>;
}
