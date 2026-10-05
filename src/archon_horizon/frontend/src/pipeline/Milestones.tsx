import {useState, type FormEvent} from "react";
import {Check, ExternalLink, Play, Plus, RefreshCw} from "lucide-react";
import {usePagedRead, useRead} from "./queries";
import {useSave} from "./Administration";
import type {Resources} from "./api";
import "./Milestones.css";

type Milestone = {key: string; title: string; children: string[]; belongs_to: string[];
  statement_status: string; proof_status: string; source_url?: string;
  milestone: {id: string; retired: boolean; contract?: {declarations: string[]}; definitions: string[];
    proof_check_id?: string; references?: {cite_key: string; locator: string}[]}};
type Baseline = {id: string; source_commit_oid: string; created_at: string};
type View = {enabled: boolean; document: {id: string; title: string; revision: number; source_commit_oid: string};
  current_baseline_id: string | null;
  nodes: Milestone[]; baselines: Baseline[]; checks: {id: string; kind: string}[];
  gates: {id: string; title: string; kind: string; url?: string}[]};
type Mission = {id: string; title: string; roadmap_document_id: string | null; status: string};
const label = (value: string) => value.replace(/_/g, " ");

export default function Milestones({accountId, projectId, documentId, writable, onNode, onRun}: {
  accountId: string; projectId: string; documentId: string; writable: boolean;
  onNode: (key: string) => void; onRun: (id: string) => void;
}) {
  const query = useRead<View>(accountId, "roadmap", projectId, `/documents/${documentId}/milestones`);
  const approve = useSave(accountId, `milestone-approval.${documentId}`);
  const launch = useSave(accountId, `milestone-launch.${documentId}`);
  const createMission = useSave(accountId, `milestone-mission.${documentId}`);
  const [note, setNote] = useState("");
  const [checkId, setCheckId] = useState("");
  const [routeGate, setRouteGate] = useState("");
  const [contractGate, setContractGate] = useState("");
  const [previous, setPrevious] = useState("");
  const [phase, setPhase] = useState("preprocessing");
  const [baselineId, setBaselineId] = useState("");
  const [missionId, setMissionId] = useState("");
  const [hosts, setHosts] = useState<string[]>([]);
  const [maxAssignments, setMaxAssignments] = useState(24);
  const missions = usePagedRead<Mission>(accountId, "runs", projectId, `/records/mission?project_id=${projectId}`, writable);
  const resources = useRead<Resources>(accountId, "resources", "global", "/resources", writable);
  const data = query.data;
  async function accept(event: FormEvent) {
    event.preventDefault();
    if (!data) return;
    await approve.save("/milestones/baselines", {document_id: documentId, expected_revision: data.document.revision,
      check_id: checkId, route_gate_id: routeGate, contract_gate_id: contractGate,
      previous_snapshot_id: previous || null, note}, "POST");
  }
  async function start(event: FormEvent) {
    event.preventDefault();
    const result = await launch.save<{id: string}>("/records/run", {mission_id: missionId, host_ids: hosts,
      max_assignments: maxAssignments, phase: phase === "formalization" ? {kind: phase, roadmap_snapshot_id: baselineId}
        : {kind: phase, roadmap_document_id: documentId}}, "POST");
    if (result) onRun(result.id);
  }
  if (query.error) return <div role="alert" className="platform-error">{query.error.message}</div>;
  if (!data) return <p className="platform-muted">Loading milestones...</p>;
  const current = data.baselines.find(item => item.id === data.current_baseline_id);
  return <section className="milestones" aria-label="Milestones">
    <div className="platform-toolbar"><h2>Milestones</h2><button className="platform-icon-button" title="Refresh milestones"
      aria-label="Refresh milestones" onClick={() => void query.refetch()}><RefreshCw size={16}/></button></div>
    <div className="milestone-table-scroll"><table><thead><tr><th>Milestone</th><th>Statement</th><th>Proof</th></tr></thead>
      <tbody>{data.nodes.map(node => <tr key={node.key}><td><button className="platform-text-button" onClick={() => onNode(node.key)}>
        {node.milestone.id}: {node.title}{node.milestone.retired ? " (retired)" : ""}</button>
        <details><summary>Contract and dependencies</summary>
          {node.source_url && <a href={node.source_url} target="_blank" rel="noreferrer"><ExternalLink size={13}/> Lean source</a>}
          {node.milestone.contract?.declarations.map(name => <code key={name}>{name}</code>)}
          {node.milestone.definitions.map(path => <code key={path}>{path}</code>)}
          {node.milestone.references?.map(ref => <p key={ref.cite_key + ref.locator}>{ref.cite_key}: {ref.locator}</p>)}
          {node.milestone.proof_check_id && <a href={`/api/v3/milestones/checks/${node.milestone.proof_check_id}`} target="_blank" rel="noreferrer"><ExternalLink size={13}/> Proof evidence</a>}
          <div>{node.children.map(key => <button key={key} className="platform-text-button" onClick={() => onNode(key)}>{key}</button>)}</div>
        </details></td><td>{label(node.statement_status)}</td><td>{label(node.proof_status)}</td></tr>)}</tbody></table></div>
    {!data.nodes.length && <p className="platform-muted">No milestones</p>}
    {current && <p className="milestone-accepted"><Check size={16}/> Baseline approved <code>{current.source_commit_oid.slice(0, 12)}</code></p>}
    {writable && data.enabled && <>
      <details className="milestone-action"><summary>Baseline approval</summary><form onSubmit={accept}>
        <fieldset disabled={approve.busy || approve.uncertain}>
          <label>Verification<select aria-label="Verification" required value={checkId} onChange={e => setCheckId(e.target.value)}><option value="">Select verification</option>
            {data.checks.filter(c => ["contract", "graph"].includes(c.kind)).map(c => <option key={c.id} value={c.id}>{c.kind} / {c.id.slice(0, 8)}</option>)}</select></label>
          <label>Route review<select aria-label="Route review" required value={routeGate} onChange={e => setRouteGate(e.target.value)}><option value="">Select route PR</option>
            {data.gates.filter(g => g.kind === "route").map(g => <option key={g.id} value={g.id}>{g.title}</option>)}</select></label>
          <label>Contract review<select aria-label="Contract review" required value={contractGate} onChange={e => setContractGate(e.target.value)}><option value="">Select contract PR</option>
            {data.gates.filter(g => g.kind === "contract").map(g => <option key={g.id} value={g.id}>{g.title}</option>)}</select></label>
          <label>Previous baseline<select aria-label="Previous baseline" value={previous} onChange={e => setPrevious(e.target.value)}><option value="">Initial approval</option>
            {data.baselines.map(b => <option key={b.id} value={b.id}>{b.source_commit_oid.slice(0, 12)}</option>)}</select></label>
          <label className="milestone-wide">Decision<textarea required value={note} onChange={e => setNote(e.target.value)} rows={3}/></label>
        </fieldset>
        <div className="milestone-evidence">
          {checkId && <a href={`/api/v3/milestones/checks/${checkId}`} target="_blank" rel="noreferrer"><ExternalLink size={13}/> Verification evidence</a>}
          {data.gates.filter(g => (g.id === routeGate || g.id === contractGate) && g.url).map(g =>
            <a key={g.id} href={g.url} target="_blank" rel="noreferrer"><ExternalLink size={13}/>{g.title}</a>)}
        </div>
        {approve.error && <p role="alert" className="platform-error">{approve.error.message}</p>}
        <button className="platform-primary" disabled={approve.busy || approve.storageBlocked || !!current && !approve.uncertain}><Check size={15}/>{approve.uncertain ? "Retry approval" : "Approve baseline"}</button>
      </form></details>
      <details className="milestone-action"><summary>Launch run</summary><form onSubmit={start}>
        <fieldset disabled={launch.busy || launch.uncertain}>
          <label>Phase<select aria-label="Phase" value={phase} onChange={e => setPhase(e.target.value)}><option value="preprocessing">Preprocessing</option>
            <option value="formalization">Formalization</option></select></label>
          {phase === "formalization" && <label>Approved baseline<select aria-label="Approved baseline" required value={baselineId} onChange={e => setBaselineId(e.target.value)}><option value="">Select baseline</option>
            {data.baselines.map(b => <option key={b.id} value={b.id}>{b.source_commit_oid.slice(0, 12)}</option>)}</select></label>}
          <label>Mission<select aria-label="Mission" required value={missionId} onChange={e => setMissionId(e.target.value)}><option value="">Select mission</option>
            {missions.data?.pages.flatMap(p => p.items).filter(m => m.roadmap_document_id === documentId && m.status === "open")
              .map(m => <option key={m.id} value={m.id}>{m.title}</option>)}</select></label>
          <label>Assignment limit<input type="number" min={1} required value={maxAssignments} onChange={e => setMaxAssignments(e.target.valueAsNumber)}/></label>
          <div className="milestone-wide"><span>Hosts</span>{resources.data?.hosts.map(host => <label className="milestone-host" key={host.id}>
            <input type="checkbox" checked={hosts.includes(host.id)} onChange={e => setHosts(e.target.checked ? [...hosts, host.id] : hosts.filter(id => id !== host.id))}/>{host.name}</label>)}</div>
        </fieldset>
        {missions.hasNextPage && <button type="button" className="platform-text-button" onClick={() => void missions.fetchNextPage()}>More missions</button>}
        <button type="button" disabled={createMission.busy || createMission.storageBlocked} onClick={() => void createMission.save<Mission>("/records/mission", {
          project_id: projectId, roadmap_document_id: documentId, title: data.document.title,
          objective: `Develop the objective in ${data.document.id}, following its selected phase and reviewed milestone contracts.`}, "POST").then(m => {if (m) setMissionId(m.id);})}><Plus size={15}/>{createMission.uncertain ? "Retry mission creation" : "Create objective mission"}</button>
        {[launch.error, createMission.error, missions.error, resources.error].filter(Boolean).map((error, i) => <p key={i} role="alert" className="platform-error">{error!.message}</p>)}
        <button className="platform-primary" disabled={launch.busy || launch.storageBlocked || !launch.uncertain && (!hosts.length || !missionId || phase === "formalization" && !baselineId)}>
          <Play size={15}/>{launch.uncertain ? "Retry launch" : "Launch run"}</button>
      </form></details>
    </>}
  </section>;
}
