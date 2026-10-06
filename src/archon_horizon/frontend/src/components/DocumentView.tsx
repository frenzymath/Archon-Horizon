import { useEffect, useMemo, useState } from "react";
import BlueprintMarkdown from "./BlueprintMarkdown";
import RoadmapMarkdown, { NodeReference } from "./RoadmapMarkdown";
import { documentBody, mathMacros, splitDocument, type DocumentMetadata } from "../utils/document";
import { bibliographyEntriesFromMarkdown } from "../utils/bibliography";
import { extractNodeReferences } from "../utils/blueprintSyntax";
import type { SourceContext, WorkspaceRepository } from "../utils/sourceLinks";
import { isProgressLabel, TagList } from "./TagList";
import "./blueprint.css";

export { TagList } from "./TagList";

type Item = Record<string, any>;

function metadataTags(metadata: DocumentMetadata): string[] {
  const tags = Array.isArray(metadata.tags) ? metadata.tags.filter((tag): tag is string => typeof tag === "string") : [];
  const title = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);
  if (typeof metadata.type === "string") tags.unshift(title(metadata.type));
  if (Array.isArray(metadata.content_types)) tags.push(...metadata.content_types.filter((entry): entry is string => typeof entry === "string").map(title));
  if (metadata.is_sketch === true) tags.push("Sketch");
  if (metadata.checks && typeof metadata.checks === "object" && !Array.isArray(metadata.checks)) {
    for (const [name, result] of Object.entries(metadata.checks)) if (result === "pass" || result === "fail") tags.push(`${["lsp", "ai"].includes(name) ? name.toUpperCase() : title(name)} ${result === "pass" ? "ok" : "failed"}`);
  }
  if (typeof metadata.lean_version === "string" && metadata.lean_version.trim()) tags.push(`Lean ${metadata.lean_version}`);
  if (Array.isArray(metadata.labels)) tags.push(...metadata.labels.filter((entry): entry is string => typeof entry === "string"));
  return tags.filter((tag) => !isProgressLabel(tag));
}

export default function DocumentView({ document: raw, nodes = [], projectId = "", onNode, className = "", showMetadata = true, showNodes = false, roadmap = false, nodeResolver, nodeSummaries, sourceContext, repository }: {
  document: string; nodes?: Item[]; projectId?: string; onNode?: (node: Item) => void; className?: string; showMetadata?: boolean; showNodes?: boolean; roadmap?: boolean; nodeResolver?: (id: string) => Promise<Item | null>;
  sourceContext?: SourceContext;
  repository?: WorkspaceRepository;
  nodeSummaries?: (ids: string[], signal: AbortSignal) => Promise<{nodes: Item[]}>;
}) {
  const parsed = useMemo(() => splitDocument(raw), [raw]);
  const body = useMemo(() => documentBody(parsed), [parsed]);
  const bibliographyEntries = useMemo(() => bibliographyEntriesFromMarkdown(body), [body]);
  const metadataNodes = useMemo(() => showNodes && Array.isArray(parsed.metadata.nodes)
    ? [...new Set(parsed.metadata.nodes.filter((id): id is string => typeof id === "string"))] : [], [parsed, showNodes]);
  const referenceIds = useMemo(() => [...new Set([...metadataNodes, ...extractNodeReferences(body)])], [body, metadataNodes]);
  const suppliedNodeIds = useMemo(() => nodes.map((node) => `${node.id || ""}:${node.label || node.metadata?.label || ""}`).sort().join("\u0000"), [nodes]);
  const [referenceNodes, setReferenceNodes] = useState<Item[]>([]);
  const [referenceLookupComplete, setReferenceLookupComplete] = useState(true);
  useEffect(() => {
    const known = new Set(nodes.flatMap((node) => [String(node.id), String(node.label || node.metadata?.label || "")].filter(Boolean)));
    const missing = referenceIds.filter((id) => !known.has(id));
    if (!nodeSummaries || !missing.length) { setReferenceNodes([]); setReferenceLookupComplete(true); return; }
    const controller = new AbortController();
    setReferenceNodes([]);
    setReferenceLookupComplete(false);
    const batches = Array.from({ length: Math.ceil(missing.length / 100) }, (_, index) => missing.slice(index * 100, index * 100 + 100));
    Promise.all(batches.map((ids) => nodeSummaries(ids, controller.signal)))
      .then((responses) => {
        if (controller.signal.aborted) return;
        const summaries = responses.flatMap((response) => Array.isArray(response?.nodes) ? response.nodes : []);
        setReferenceNodes(summaries);
        setReferenceLookupComplete(true);
      })
      .catch(() => { if (!controller.signal.aborted) setReferenceLookupComplete(true); });
    return () => controller.abort();
  }, [projectId, body, referenceIds, suppliedNodeIds, nodeSummaries]);
  const referenceAwareNodes = useMemo(() => {
    const byId = new Map<string, Item>();
    for (const node of [...nodes, ...referenceNodes]) if (node?.id && !byId.has(String(node.id))) byId.set(String(node.id), node);
    return [...byId.values()];
  }, [nodes, referenceNodes]);
  const markdownProps = { nodes: referenceAwareNodes, projectId, onNode, bibliographyEntries, mathMacros: mathMacros(parsed.metadata), nodeResolver, referenceLookupComplete };
  const renderMarkdown = (content: string) => roadmap
    ? <RoadmapMarkdown content={content} {...markdownProps} title={typeof parsed.metadata.title === "string" ? parsed.metadata.title : undefined} />
    : <BlueprintMarkdown content={content} {...markdownProps} sourceContext={sourceContext} repository={repository} />;
  return <div className={`platform-document-view ${className}`}>
    {parsed.error && <div className="platform-document-error" role="alert"><strong>Invalid document metadata</strong><p>{parsed.error}</p></div>}
    {showMetadata && <TagList tags={metadataTags(parsed.metadata)} />}
    {showNodes && <div className="platform-document-nodes" aria-label="Mission nodes"><strong>Nodes</strong>{metadataNodes.length
      ? metadataNodes.map(id => <NodeReference key={id} id={id} {...markdownProps}>{id}</NodeReference>)
      : <span className="platform-muted">None</span>}</div>}
    {renderMarkdown(body)}
  </div>;
}
