import { lazy, Suspense, useCallback, useEffect, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, CircleDot, ExternalLink, GitBranch, Map as MapIcon, Network, Plus, RefreshCw, Search, Settings2, Star } from "lucide-react";
import MathTitle from "../components/MathTitle";
import RecordDates from "../components/RecordDates";
import { NodeProgressLabels } from "../components/TagList";
import { nodeHasLabel, progressLabelTitles } from "../formalizationGraph";
import { composeDocument } from "../utils/document";
import { request, type Project, type Run } from "./api";
import { useRead } from "./queries";
import RunPhase from "./RunPhase";
const DesktopMissions = lazy(() => import("./DesktopMissions"));
const Milestones = lazy(() => import("./Milestones"));
const DesktopReferences = lazy(() => import("./DesktopReferences"));

const DocumentView = lazy(() => import("../components/DocumentView"));
const FormalizationDAG = lazy(() => import("../components/FormalizationDAG"));
type Item = Record<string, any>;
type Props = {accountId: string; projects: Project[]; projectId: string; view: string;
  selection: {node?: string; objective?: string; reference?: string}; writable: boolean; admin: boolean;
  onNavigate: (values: Record<string, string>) => void; onCreate: () => void; onEditProject?: () => void};
const date = (value?: string) => value ? new Date(value).toLocaleString() : "";
const pathFor = (project: string) => `/projects/${encodeURIComponent(project)}/dashboard`;

function Empty({children}: {children: ReactNode}) { return <div className="platform-empty">{children}</div>; }
function IconButton({label, onClick, disabled, children}: {label: string; onClick: () => void; disabled?: boolean; children: ReactNode}) {
  return <button type="button" className="platform-icon-button" title={label} aria-label={label} onClick={onClick} disabled={disabled}>{children}</button>;
}
function Heading({title, item, labels, children}: {title: string; item?: Item; labels?: string[]; children?: ReactNode}) {
  return <div className="platform-document-heading"><div><h1><MathTitle title={title} metadata={item?.metadata} /></h1>
    {labels && <NodeProgressLabels labels={labels} className="platform-heading-labels" />}
    {item && <div className="platform-document-dates"><RecordDates item={item} /></div>}</div>
    {children && <div className="platform-document-actions">{children}</div>}</div>;
}
function Pagination({page, total, onChange}: {page: number; total: number; onChange: (page: number) => void}) {
  if (!total) return null;
  const pages = Math.ceil(total / 50);
  return <div className="platform-node-pagination" role="navigation" aria-label="Directory pagination">
    <span>Showing {page * 50 + 1}-{Math.min((page + 1) * 50, total)} of {total}</span>
    <div className="platform-node-pagination-controls"><IconButton label="Previous page" disabled={!page} onClick={() => onChange(page - 1)}><ChevronLeft size={16} /></IconButton>
      <span aria-live="polite">Page {page + 1} of {pages}</span><IconButton label="Next page" disabled={page + 1 >= pages} onClick={() => onChange(page + 1)}><ChevronRight size={16} /></IconButton></div>
  </div>;
}
function SourceDocument({projectId, item, nodes = [], roadmap = false, onNode, target = ""}: {projectId: string; item: Item; nodes?: Item[]; roadmap?: boolean; onNode: (node: Item) => void; target?: string}) {
  const base = pathFor(projectId);
  const nodeSummaries = useCallback((ids: string[], signal: AbortSignal) => request<{nodes: Item[]}>(
    `${base}/nodes/resolve?identifiers=${encodeURIComponent(ids.join(","))}${target ? `&target_repository_id=${target}` : ""}`, {signal}), [base, target]);
  const nodeResolver = useCallback(async (id: string) => (await request<{node: Item}>(`${base}/nodes/${encodeURIComponent(id)}${target ? `?target_repository_id=${target}` : ""}`)).node, [base, target]);
  return <Suspense fallback={<Empty>Loading document...</Empty>}><DocumentView roadmap={roadmap}
    document={composeDocument({...item.metadata, title: item.title}, item.markdown || item.description || "")}
    nodes={nodes} projectId={projectId} onNode={onNode} nodeSummaries={nodeSummaries} nodeResolver={nodeResolver} /></Suspense>;
}
function NodeContent({accountId, projectId, identifier, onNode, target = ""}: {accountId: string; projectId: string; identifier: string; onNode: (node: Item) => void; target?: string}) {
  const detail = useRead<{node: Item; nodes: Item[]}>(accountId, "roadmap", projectId, `${pathFor(projectId)}/nodes/${encodeURIComponent(identifier)}${target ? `?target_repository_id=${target}` : ""}`, !!identifier);
  if (detail.error) return <div role="alert" className="platform-error">{detail.error.message}</div>;
  if (!detail.data) return <Empty>Loading node...</Empty>;
  const {node, nodes} = detail.data;
  const children = nodes.filter(item => node.children?.includes(item.id));
  return <section className="platform-node-repo" aria-label="Node repository">
    <div className="platform-node-repo-meta">{node.source_url && <a className="platform-node-repo-link" href={node.source_url} target="_blank" rel="noreferrer"><ExternalLink size={13} /> Forgejo</a>}</div>
    {node.implementation && <p className="platform-muted">Review: {String(node.implementation.review).replace(/_/g, " ")}{node.implementation.stale ? " / Content changed; previous evidence is historical" : ""}</p>}
    <SourceDocument item={node} nodes={nodes} projectId={projectId} onNode={onNode} target={target} />
    {!!children.length && <section className="platform-blueprint-section platform-node-children" aria-label="Child nodes"><h3>Children</h3><div className="platform-child-links">
      {children.map(item => <button key={item.id} className="platform-child-link" onClick={() => onNode(item)}><CircleDot size={14} /><MathTitle title={item.title} metadata={item.metadata} /></button>)}
    </div></section>}
  </section>;
}

export default function DesktopProjects({accountId, projects, projectId, view, selection, writable, admin, onNavigate, onCreate, onEditProject}: Props) {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [label, setLabel] = useState("");
  const [nodeType, setNodeType] = useState("");
  const [nodeTypes, setNodeTypes] = useState<string[]>([]);
  const [milestone, setMilestone] = useState("");
  const [page, setPage] = useState(0);
  const [target, setTarget] = useState("");
  useEffect(() => {setTarget(""); setNodeTypes([]);}, [projectId]);
  const targetQuery = target ? `&target_repository_id=${encodeURIComponent(target)}` : "";
  const targets = useRead<{target_repository_id: string | null; targets: Item[]}>(accountId, "roadmap", projectId,
    `${pathFor(projectId)}/graph-targets`, !!projectId && ["graph", "nodes", "node", "node-dag", "roadmap"].includes(view));
  useEffect(() => {setSearch(""); setQuery(""); setPage(0); setLabel(""); setNodeType(""); setMilestone("");}, [projectId, view]);
  useEffect(() => {const timer = window.setTimeout(() => {setQuery(search); setPage(0);}, 180); return () => window.clearTimeout(timer);}, [search]);
  const base = pathFor(projectId);
  const project = projects.find(item => item.id === projectId);
  const overview = useRead<Item>(accountId, "projects", projectId, `${base}/overview`, !!projectId && view === "overview");
  const runs = useRead<{runs: Run[]}>(accountId, "assignments", projectId,
    `/dashboard/activity/runs?project_id=${encodeURIComponent(projectId)}&limit=5`, !!projectId && view === "overview");
  const directory = useRead<{nodes: Item[]; total: number; types: string[]}>(accountId, "roadmap", projectId,
    `${base}/nodes?search=${encodeURIComponent(query)}&label=${encodeURIComponent(label)}&node_type=${encodeURIComponent(nodeType)}${milestone ? `&milestone=${milestone}` : ""}&offset=${page * 50}&limit=50${targetQuery}`, !!projectId && view === "nodes");
  // Keep the project-wide selector usable while another filtered page loads.
  useEffect(() => {if (directory.data?.types) setNodeTypes(directory.data.types);}, [directory.data]);
  const objectives = useRead<{items: Item[]}>(accountId, "roadmap", projectId, `${base}/objectives`, !!projectId && view === "roadmap" && !selection.objective);
  const objective = useRead<Item>(accountId, "roadmap", projectId, `${base}/objectives/${selection.objective || ""}`, !!projectId && view === "roadmap" && !!selection.objective);
  const node = useRead<{node: Item; nodes: Item[]}>(accountId, "roadmap", projectId, `${base}/nodes/${encodeURIComponent(selection.node || "")}?${targetQuery.slice(1)}`,
    !!projectId && ["node", "node-dag"].includes(view) && !!selection.node);
  const graph = useRead<Item>(accountId, "roadmap", projectId, `${base}/graph?${view === "node-dag" ? `focus=${encodeURIComponent(node.data?.node.id || "")}` : ""}${targetQuery}`,
    !!projectId && (view === "graph" || (view === "node-dag" && !!node.data)));
  const go = (next: Record<string, string>) => onNavigate({tab: "projects", project: projectId, node: "", reference: "", objective: ["node", "node-dag"].includes(next.view) ? selection.objective || "" : "", ...next});
  const openNode = (item: Item) => go({view: "node", node: String(item.id)});
  const activeReads = view === "overview" ? [overview] : view === "nodes" ? [directory]
    : view === "roadmap" ? [selection.objective ? objective : objectives]
    : ["missions", "references"].includes(view) ? [] : view === "graph" ? [graph] : [node, graph];
  const error = activeReads.find(item => item.error)?.error;
  const searchField = (name: string) => <label className="platform-search-field"><Search size={16} /><input aria-label={`Search ${name}`} placeholder={`Search ${name}`} value={search} onChange={event => setSearch(event.target.value)} /></label>;
  const current = overview.data || project;
  const selectedNode = node.data?.node;
  return <>
    {error && <div role="alert" className="platform-error">{error.message}</div>}
    {!projectId ? <section className="platform-project-directory">
      <div className="platform-toolbar">{searchField("projects")}{writable && admin && <button className="platform-primary" onClick={onCreate}><Plus size={15} /> New project</button>}</div>
      <div className="platform-directory-label"><span>{projects.length} projects</span><span>Activity</span></div>
      {projects.filter(item => `${item.title} ${item.description || ""}`.toLowerCase().includes(search.toLowerCase())).map(item => <button className="platform-project-directory-row" key={item.id} onClick={() => go({project: item.id, view: "overview"})}>
        <Network size={23} /><span className="platform-project-directory-copy"><strong>{item.title}</strong><small>{item.description || "Formalization project"}</small></span><ChevronRight size={17} />
      </button>)}
      {!projects.length && <Empty><Network size={32} /><strong>No projects</strong></Empty>}
    </section> : <>
      {["graph", "nodes", "node", "node-dag", "roadmap"].includes(view) && <div className="platform-toolbar">
        <label>Implementation <select aria-label="Graph target repository" className="platform-filter-select platform-graph-target"
          value={target || targets.data?.target_repository_id || ""} onChange={event => {setTarget(event.target.value); setPage(0);}}>
          {!targets.data?.target_repository_id && !target && <option value="">Select repository</option>}
          {targets.data?.targets.map(item => <option key={item.id} value={item.id}>{item.slug} ({item.purpose})</option>)}
        </select></label>
        {targets.error && <span role="alert">{targets.error.message}</span>}
      </div>}
      {view === "overview" && <section className="platform-project-overview"><Heading title={current?.title || "Project"} item={current}>
        {writable && onEditProject && <IconButton label="Edit overview" onClick={onEditProject}><Settings2 size={16} /></IconButton>}
      </Heading>
        <section className="desktop-project-runs" aria-label="Recent runs"><div className="platform-toolbar"><h2>Recent runs</h2>
          <button className="platform-text-button" onClick={() => onNavigate({tab: "activity", project: projectId, run: "", session: ""})}>All runs<ChevronRight size={14} /></button></div>
          {runs.error && <div role="alert" className="platform-error">{runs.error.message}</div>}
          {runs.data?.runs.map(item => <button key={item.id} className="desktop-project-run" onClick={() => onNavigate({tab: "activity", project: projectId, run: item.id, session: ""})}>
            <span className="desktop-project-run-title"><strong>Run #{item.number}{item.title ? ` / ${item.title}` : ""}</strong><RunPhase phase={item.phase} /></span>
            <span className="desktop-project-run-status">{item.status === "active" ? "Running" : item.status.charAt(0).toUpperCase() + item.status.slice(1)}</span><ChevronRight size={15} />
          </button>)}
          {!runs.error && !runs.data?.runs.length && <p className="platform-muted">{runs.isPending ? "Loading runs..." : "No runs yet"}</p>}
        </section>
        {current && <SourceDocument projectId={projectId} item={current} onNode={openNode} />}</section>}
      {view === "roadmap" && <section className="platform-roadmaps">{selection.objective ? <>
        <div className="platform-roadmap-tools"><button className="platform-text-button" onClick={() => go({view: "roadmap"})}><ChevronLeft size={14} />All objectives</button>
          {objective.data?.source_url && <a className="platform-node-repo-link" href={objective.data.source_url} target="_blank" rel="noreferrer"><ExternalLink size={14} /> Forgejo</a>}</div>
        {objective.data ? <><Heading title={objective.data.title} item={objective.data} /><SourceDocument item={objective.data} roadmap projectId={projectId} onNode={openNode} target={target} />
          {project?.workflow === "milestones" && <Suspense fallback={<Empty>Loading milestones...</Empty>}>
            <Milestones key={objective.data.id} accountId={accountId} projectId={projectId} documentId={objective.data.id} writable={writable}
              onNode={key => openNode({id: key})} onRun={id => onNavigate({tab: "activity", project: projectId, run: id, session: ""})}/>
          </Suspense>}</> : <Empty>Loading objective...</Empty>}
      </> : <><Heading title="Objectives" /><div className="platform-toolbar">{searchField("objectives")}</div>
        <div className="platform-directory-label"><span>{objectives.data?.items.length || 0} objectives</span><span>Updated</span></div>
        {objectives.data?.items.filter(item => item.title.toLowerCase().includes(search.toLowerCase())).map(item => <button className="platform-claim-row" key={item.id} onClick={() => go({view: "roadmap", objective: item.id})}>
          <MapIcon size={17} /><span className="platform-claim-copy"><strong>{item.title}</strong><small>Revision {item.revision}</small></span><small className="platform-updated">{date(item.updated_at)}</small><ChevronRight size={16} />
        </button>)}{!objectives.data?.items.length && <Empty>{objectives.isLoading ? "Loading objectives..." : "No objectives"}</Empty>}
      </>}</section>}
      {view === "nodes" && <section><Heading title="Nodes" /><div className="platform-toolbar">{searchField("nodes")}
        <label>Milestones <select aria-label="Milestone filter" className="platform-filter-select" value={milestone} onChange={event => {setMilestone(event.target.value); setPage(0);}}>
          <option value="">All nodes</option><option value="true">Milestones only</option><option value="false">Other nodes</option></select></label>
        <label>Type <select aria-label="Node type" className="platform-filter-select" value={nodeType} onChange={event => {setNodeType(event.target.value); setPage(0);}}>
          <option value="">All types</option>{nodeTypes.map(value => <option key={value} value={value}>{value.charAt(0).toUpperCase() + value.slice(1).replace(/_/g, " ")}</option>)}</select></label>
        <label>Progress <select aria-label="Node label" className="platform-filter-select" value={label} onChange={event => {setLabel(event.target.value); setPage(0);}}>
          <option value="">All progress</option>{Object.entries(progressLabelTitles).map(([value, title]) => <option key={value} value={value}>{title}</option>)}</select></label>
        {(search || label || nodeType || milestone) && <button type="button" className="platform-text-button" onClick={() => {setSearch(""); setQuery(""); setLabel(""); setNodeType(""); setMilestone(""); setPage(0);}}>Clear filters</button>}
      </div>
        <div className="platform-directory-label"><span>{directory.data?.total || 0} nodes</span><span>Labels</span></div>
        <div className="platform-claim-rows">{directory.data?.nodes.map(item => <div className="platform-node-directory-item" key={item.id}><div className="platform-node-list-row">
          <button className="platform-node-list-link" onClick={() => openNode(item)}>{nodeHasLabel(item, "milestone") ? <Star size={17} aria-label="Milestone" /> : <CircleDot size={17} />}<span><strong><MathTitle title={item.title} metadata={item.metadata} /></strong><small>{item.kind} <code className="platform-node-label">{item.label}</code></small><RecordDates item={item} /></span></button>
          <NodeProgressLabels labels={item.labels || []} /></div></div>)}</div>
        {!directory.data?.nodes.length && <Empty>{directory.isLoading ? "Loading nodes..." : "No matching nodes"}</Empty>}
        <Pagination page={page} total={directory.data?.total || 0} onChange={setPage} />
      </section>}
      {["node", "node-dag"].includes(view) && <section>{selectedNode ? <><Heading title={selectedNode.title} item={selectedNode} labels={view === "node" ? selectedNode.labels : undefined}>
        <IconButton label={view === "node" ? "Open node DAG" : "Refresh graph"} onClick={() => view === "node" ? go({view: "node-dag", node: selectedNode.id}) : void graph.refetch()}>{view === "node" ? <GitBranch size={16} /> : <RefreshCw size={16} />}</IconButton>
      </Heading><code className="platform-node-label">{selectedNode.label}</code>{view === "node" && <NodeContent accountId={accountId} projectId={projectId} identifier={selectedNode.id} onNode={openNode} target={target} />}</> : <Empty>Loading node...</Empty>}</section>}
      {(view === "graph" || view === "node-dag") && <section>{view === "graph" && <Heading title="Graph"><IconButton label="Refresh graph" onClick={() => void graph.refetch()}><RefreshCw size={16} /></IconButton></Heading>}
        {graph.data ? <Suspense fallback={<Empty>Loading graph...</Empty>}><FormalizationDAG graph={graph.data} focusId={view === "node-dag" ? selectedNode?.id : ""}
          onOpenNode={openNode} renderNodeDetails={item => <NodeContent accountId={accountId} projectId={projectId} identifier={item.id} onNode={openNode} target={target} />} /></Suspense> : <Empty>Loading graph...</Empty>}
      </section>}
      {view === "missions" && <Suspense fallback={<Empty>Loading missions...</Empty>}><DesktopMissions key={`${accountId}:${projectId}`} accountId={accountId} projectId={projectId} writable={writable} onNavigate={onNavigate}
        renderDocument={(mission, close) => <SourceDocument projectId={projectId} item={{...mission, markdown: mission.objective}} roadmap onNode={item => {close(); openNode(item);}} />} /></Suspense>}
      {view === "references" && <Suspense fallback={<Empty>Loading references...</Empty>}><DesktopReferences key={`${accountId}:${projectId}`} accountId={accountId} projectId={projectId} referenceId={selection.reference} writable={writable}
        onSelect={reference => go({view: "references", reference})}/></Suspense>}
    </>}
  </>;
}
