import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Check, ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, ListTodo, Plus, RotateCcw, Search, Settings2, X } from "lucide-react";
import RecordDates from "../components/RecordDates";
import { missionTree } from "../utils/missionTree";
import { request, type Operation } from "./api";
import { useRead } from "./queries";
import { useSave } from "./Administration";

type Mission = {id: string; title: string; number: number; objective?: string; parent_id: string | null; parent_title?: string | null;
  acceptance_criteria?: string[]; delegation_note?: string | null; max_open_children?: number;
  status: string; revision: number; child_count?: number; created_at: string; updated_at: string;
  latest_run?: {id: string; number: number; status: string} | null};
type MissionPage = {items: Mission[]; match_ids: string[]; total: number; next_offset: number | null};
type Active = {mode: "edit" | "preview" | "status"; id?: string; parent?: string; parentTitle?: string} | null;
type Draft = {title: string; objective: string; parent_id: string; parent_title: string; expected_revision: number;
  acceptance_criteria: string; delegation_note: string; max_open_children: number};
const basePath = (project: string) => `/projects/${encodeURIComponent(project)}/dashboard`;
function Icon({label, onClick, disabled, children}: {label: string; onClick: () => void; disabled?: boolean; children: ReactNode}) {
  return <button type="button" className="platform-icon-button" title={label} aria-label={label} onClick={onClick} disabled={disabled}>{children}</button>;
}
function Modal({title, children, onClose}: {title: string; children: ReactNode; onClose: () => void}) {
  useEffect(() => {const listener = (event: KeyboardEvent) => {if (event.key === "Escape") onClose();}; window.addEventListener("keydown", listener); return () => window.removeEventListener("keydown", listener);}, [onClose]);
  return <div className="platform-overlay" onMouseDown={event => {if (event.target === event.currentTarget) onClose();}}><div className="platform-modal" role="dialog" aria-modal="true" aria-label={title}>
    <div className="platform-modal-head"><h2>{title}</h2><Icon label="Close" onClick={onClose}><X size={17} /></Icon></div>{children}</div></div>;
}
function readDraft<T>(key: string, fallback: T): T {
  try {
    const value = sessionStorage.getItem(key);
    if (!value) return fallback;
    const parsed = JSON.parse(value);
    if (fallback === null) return parsed && ["edit", "preview", "status"].includes(parsed.mode) && (!parsed.id || typeof parsed.id === "string") ? parsed : fallback;
    if (typeof fallback === "string") return typeof parsed === "string" ? parsed as T : fallback;
    if (!parsed || typeof parsed !== "object" || Object.entries(fallback as object).some(([key, value]) => typeof parsed[key] !== typeof value)) return fallback;
    return parsed as T;
  } catch {return fallback;}
}
function MissionForm({accountId, projectId, record, parent, parentTitle, onClose}: {accountId: string; projectId: string; record?: Mission; parent?: string; parentTitle?: string; onClose: () => void}) {
  const identity = record?.id || `new-${parent || "root"}`;
  const draftKey = `horizon.pipeline.mission-draft.${accountId}.${projectId}.${identity}`;
  const savedValues = (): Draft => ({title: record?.title || "", objective: record?.objective || "", parent_id: record?.parent_id || parent || "",
    parent_title: record?.parent_title || parentTitle || "", expected_revision: record?.revision || 1,
    acceptance_criteria: (record?.acceptance_criteria || []).join("\n"), delegation_note: record?.delegation_note || "",
    max_open_children: record?.max_open_children || 8});
  const [draft, setDraft] = useState<Draft>(() => readDraft(draftKey, savedValues()));
  const [storageError, setStorageError] = useState("");
  const [parentSearch, setParentSearch] = useState("");
  const [parentQuery, setParentQuery] = useState("");
  const action = useSave(accountId, `mission-edit.${projectId}.${identity}`);
  useEffect(() => {try {sessionStorage.setItem(draftKey, JSON.stringify(draft)); setStorageError("");} catch {setStorageError("Draft storage is unavailable. Keep this editor open until the save is confirmed.");}}, [draftKey, draft]);
  useEffect(() => {const timer = window.setTimeout(() => setParentQuery(parentSearch), 180); return () => window.clearTimeout(timer);}, [parentSearch]);
  const parents = useRead<MissionPage>(accountId, "runs", projectId, `${basePath(projectId)}/missions?search=${encodeURIComponent(parentQuery)}&limit=50`);
  const selectedParent = useRead<Mission>(accountId, "runs", projectId,
    `${basePath(projectId)}/missions/${encodeURIComponent(draft.parent_id)}`, !!draft.parent_id);
  const choices = parents.data?.items.filter(item => item.id !== record?.id) || [];
  const locked = action.busy || action.uncertain;
  const changed = Boolean(record && draft.expected_revision !== record.revision);
  const needsParentRevision = Boolean(draft.parent_id && (!record || draft.parent_id !== record.parent_id));
  const incompleteContract = Boolean(draft.parent_id && (!draft.acceptance_criteria.trim() || !draft.delegation_note.trim()));
  const update = (values: Partial<Draft>) => setDraft(current => ({...current, ...values}));
  return <form className="platform-editor" onSubmit={async event => {
    event.preventDefault();
    const body = {title: draft.title, objective: draft.objective, parent_id: draft.parent_id || null,
      acceptance_criteria: draft.acceptance_criteria.split("\n").map(value => value.trim()).filter(Boolean),
      ...(draft.delegation_note.trim() ? {delegation_note: draft.delegation_note.trim()} : {}),
      max_open_children: draft.max_open_children,
      ...(needsParentRevision ? {expected_parent_revision: selectedParent.data?.revision} : {}),
      ...(record ? {expected_revision: draft.expected_revision} : {project_id: projectId})};
    const saved = await action.save<Mission>(record ? `/missions/${record.id}` : "/records/mission", body, record ? "PATCH" : "POST");
    if (saved) {sessionStorage.removeItem(draftKey); onClose();}
  }}>
    <label>Title<input required value={draft.title} disabled={locked} onChange={event => update({title: event.target.value})} /></label>
    <label>Objective<textarea required rows={12} value={draft.objective} disabled={locked} onChange={event => update({objective: event.target.value})} /></label>
    <label>Acceptance criteria<textarea required={!!draft.parent_id} rows={4} value={draft.acceptance_criteria} disabled={locked} onChange={event => update({acceptance_criteria: event.target.value})} /></label>
    <label>Delegation rationale<textarea required={!!draft.parent_id} rows={3} value={draft.delegation_note} disabled={locked} onChange={event => update({delegation_note: event.target.value})} /></label>
    <label>Open child limit<input required type="number" min={1} step={1} value={draft.max_open_children} disabled={locked} onChange={event => update({max_open_children: Number(event.target.value)})} /></label>
    <label>Find a parent mission<input type="search" value={parentSearch} disabled={locked} placeholder="Search mission titles or objectives" onChange={event => setParentSearch(event.target.value)} /></label>
    <label>Parent mission<select aria-label="Parent mission" value={draft.parent_id} disabled={locked} onChange={event => update({parent_id: event.target.value, parent_title: choices.find(item => item.id === event.target.value)?.title || ""})}>
      <option value="">None / top-level mission</option>
      {draft.parent_id && !choices.some(item => item.id === draft.parent_id) && <option value={draft.parent_id}>{draft.parent_title || "Current parent mission"}</option>}
      {choices.map(item => <option key={item.id} value={item.id}>#{item.number} {item.title}</option>)}</select></label>
    {parents.error && <p className="platform-error" role="alert">{parents.error.message}</p>}
    {selectedParent.error && <p className="platform-error" role="alert">{selectedParent.error.message}</p>}
    <label>Status<input readOnly value={record?.status || "open"} /></label>
    {changed && <div className="platform-stale-evidence" role="status"><p>The saved mission changed. Your draft is retained.</p>
      <details><summary>Current saved revision {record!.revision}</summary><h3>{record!.title}</h3><p>{record!.parent_title || "Top-level mission"}</p><pre>{record!.objective}</pre></details>
      <button type="button" className="platform-small-button" disabled={locked} onClick={() => update({expected_revision: record!.revision})}>Keep my draft against revision {record!.revision}</button>
      <button type="button" className="platform-text-button" disabled={locked} onClick={() => {setDraft(savedValues()); action.reset();}}>Reload saved values</button>
    </div>}
    {storageError && <p className="platform-error" role="alert">{storageError}</p>}
    {action.error && <p className="platform-error" role="alert">{action.error.message}</p>}
    <div className="platform-editor-actions"><button type="button" className="platform-small-button" disabled={action.busy} onClick={onClose}>Close</button>
      <button type="submit" className="platform-primary" disabled={action.busy || action.storageBlocked || (!action.uncertain && (changed || incompleteContract || (needsParentRevision && !selectedParent.data) || draft.max_open_children < 1 || !draft.title.trim() || !draft.objective.trim()))}>
        {action.busy ? "Saving..." : action.uncertain ? "Retry saved request" : record ? "Save mission" : "Create mission"}</button></div>
  </form>;
}
function StatusForm({accountId, projectId, record, onClose}: {accountId: string; projectId: string; record: Mission; onClose: () => void}) {
  const key = `horizon.pipeline.mission-status.${accountId}.${record.id}`;
  const [note, setNote] = useState(() => readDraft(key, ""));
  const [error, setError] = useState("");
  const action = useSave(accountId, `mission-status.${projectId}.${record.id}`);
  const [decision, setDecision] = useState({complete: record.status === "open", revision: record.revision});
  const complete = decision.complete;
  const changed = decision.revision !== record.revision;
  return <form className="platform-editor" onSubmit={async event => {
    event.preventDefault(); setError("");
    const result = await action.save<Operation>("/commands", {operation: complete ? "complete_mission" : "reopen_mission",
      target_id: record.id, expected_revision: decision.revision, args: {note}}, "POST");
    if (result?.status === "completed") {sessionStorage.removeItem(key); onClose();}
    else if (result) setError(result.error || "The mission status could not be changed.");
  }}>
    <p><strong>{record.title}</strong></p><label>Status<input readOnly value={`${record.status} -> ${complete ? "completed" : "open"}`} /></label>
    <label>Reason<textarea required rows={5} disabled={action.busy || action.uncertain} value={note} onChange={event => {setNote(event.target.value); try {sessionStorage.setItem(key, JSON.stringify(event.target.value));} catch { /* The durable request still protects a submitted decision. */ }}} /></label>
    {changed && <div className="platform-stale-evidence"><p>The mission changed. Its current status is {record.status}.</p><button type="button" className="platform-small-button" disabled={action.busy || action.uncertain} onClick={() => setDecision(current => ({...current, revision: record.revision}))}>Use current revision {record.revision}</button></div>}
    {(action.error || error) && <p className="platform-error" role="alert">{action.error?.message || error}</p>}
    <div className="platform-editor-actions"><button className="platform-primary" disabled={action.busy || action.storageBlocked || (!action.uncertain && (changed || !note.trim()))}>{action.uncertain ? "Retry saved request" : complete ? "Mark completed" : "Reopen mission"}</button></div>
  </form>;
}

export default function DesktopMissions({accountId, projectId, writable, onNavigate, renderDocument}: {accountId: string; projectId: string; writable: boolean; onNavigate: (values: Record<string, string>) => void; renderDocument: (mission: Mission, close: () => void) => ReactNode}) {
  const activeKey = `horizon.pipeline.active-mission-editor.${accountId}.${projectId}`;
  const [active, setActive] = useState<Active>(() => readDraft(activeKey, null));
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [collapsed, setCollapsed] = useState(new Set<string>());
  useEffect(() => {const timer = window.setTimeout(() => {setQuery(search); setCollapsed(new Set());}, 180); return () => window.clearTimeout(timer);}, [search]);
  const activate = (value: Active) => {setActive(value); try {if (value) sessionStorage.setItem(activeKey, JSON.stringify(value)); else sessionStorage.removeItem(activeKey);} catch { /* Forms independently protect submitted requests. */ }};
  const listing = useInfiniteQuery({queryKey: ["pipeline", accountId, "runs", projectId, "mission-tree", query, status], initialPageParam: 0,
    queryFn: ({pageParam, signal}) => request<MissionPage>(`${basePath(projectId)}/missions?search=${encodeURIComponent(query)}&status=${encodeURIComponent(status)}&offset=${pageParam}&limit=50`, {signal}),
    getNextPageParam: (page: MissionPage) => page.next_offset ?? undefined});
  const detail = useRead<Mission>(accountId, "runs", projectId, `${basePath(projectId)}/missions/${active?.id || ""}`, !!active?.id);
  const data = useMemo(() => {
    const byId = new Map<string, Mission>(); const matches = new Set<string>();
    for (const page of listing.data?.pages || []) {for (const id of page.match_ids) matches.add(id); for (const mission of page.items) {if ((byId.get(mission.id)?.revision || 0) <= mission.revision) byId.set(mission.id, mission);}}
    return {missions: [...byId.values()], matches};
  }, [listing.data]);
  const tree = useMemo(() => missionTree(data.missions, item => data.matches.has(item.id), collapsed), [data, collapsed]);
  const toggle = (id: string) => setCollapsed(current => {const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next;});
  const close = () => activate(null);
  return <section className="platform-missions">
    <div className="platform-document-heading"><div><h1>Missions</h1></div><div className="platform-document-actions">
      <Icon label="Collapse all missions" onClick={() => setCollapsed(new Set(tree.branches))}><ChevronsDownUp size={16} /></Icon>
      <Icon label="Expand all missions" onClick={() => setCollapsed(new Set())}><ChevronsUpDown size={16} /></Icon></div></div>
    <div className="platform-mission-toolbar"><label className="platform-search-field"><Search size={16} /><input aria-label="Search missions" placeholder="Search missions" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <div className="platform-mission-filters"><select className="platform-filter-select" aria-label="Mission status" value={status} onChange={event => {setStatus(event.target.value); setCollapsed(new Set());}}><option value="">All statuses</option>{["open", "completed", "cancelled"].map(value => <option key={value}>{value}</option>)}</select></div>
      {writable && <button className="platform-primary" onClick={() => activate({mode: "edit"})}><Plus size={14} /> Mission</button>}</div>
    {listing.error && <div className="platform-error" role="alert">{listing.error.message}</div>}
    <div className="platform-mission-summary"><span>{data.matches.size} of {listing.data?.pages[0]?.total || 0} missions</span><span>Work / state / activity</span></div>
    <div className="platform-claim-rows platform-mission-tree">{tree.rows.map(({mission, depth, childCount, matches}) => <div className="platform-node-directory-item platform-mission-row" key={mission.id} data-mission-id={mission.id} style={{"--mission-depth": Math.min(depth, 6)} as CSSProperties}>
      <div className="platform-mission-toggle">{childCount ? <Icon label={`${collapsed.has(mission.id) ? "Expand" : "Collapse"} ${mission.title}`} onClick={() => toggle(mission.id)}>{collapsed.has(mission.id) ? <ChevronRight size={16} /> : <ChevronDown size={16} />}</Icon> : <ListTodo size={16} />}</div>
      <div className="platform-mission-copy"><button className="platform-node-list-link" aria-label={`Preview ${mission.title}`} onClick={() => activate({mode: "preview", id: mission.id})}><span><strong>{mission.title}</strong><small>#{mission.number}{!!mission.child_count && <> / {mission.child_count} child missions</>}{!matches && <> / Parent mission</>}</small><RecordDates item={mission} /></span></button></div>
      <div className="platform-mission-state"><span className={`platform-status ${mission.status === "completed" ? "good" : "quiet"}`}>{mission.status}</span>{mission.latest_run && <button className="platform-text-button" onClick={() => onNavigate({tab: "activity", project: projectId, run: mission.latest_run!.id, session: "", view: "", node: "", objective: ""})}>Run #{mission.latest_run.number} / {mission.latest_run.status}</button>}</div>
      {writable && <div className="platform-mission-actions"><Icon label={`Create child of ${mission.title}`} onClick={() => activate({mode: "edit", parent: mission.id, parentTitle: mission.title})}><Plus size={15} /></Icon><Icon label={`Edit ${mission.title}`} onClick={() => activate({mode: "edit", id: mission.id})}><Settings2 size={15} /></Icon><Icon label={`${mission.status === "open" ? "Complete" : "Reopen"} ${mission.title}`} onClick={() => activate({mode: "status", id: mission.id})}>{mission.status === "open" ? <Check size={15} /> : <RotateCcw size={15} />}</Icon></div>}
    </div>)}</div>
    {!tree.rows.length && <div className="platform-mission-empty"><ListTodo size={28} /><strong>{listing.isLoading ? "Loading missions..." : "No matching missions"}</strong></div>}
    {listing.hasNextPage && <div className="platform-node-pagination"><button type="button" className="platform-small-button" disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>{listing.isFetchingNextPage ? "Loading..." : "Load more missions"}</button></div>}
    {active && <Modal title={active.mode === "edit" ? active.id ? "Edit mission" : "New mission" : active.mode === "status" ? "Change mission status" : detail.data?.title || "Mission"} onClose={close}>
      {detail.error && <div className="platform-error" role="alert">{detail.error.message}</div>}
      {active.id && !detail.data ? <div className="platform-empty">Loading mission...</div> : active.mode === "edit" && writable ? <MissionForm key={active.id || `new-${active.parent || "root"}`} accountId={accountId} projectId={projectId} record={active.id ? detail.data : undefined} parent={active.parent} parentTitle={active.parentTitle} onClose={close} />
        : active.mode === "status" && writable && detail.data ? <StatusForm key={active.id} accountId={accountId} projectId={projectId} record={detail.data} onClose={close} />
        : detail.data && <><RecordDates item={detail.data} />{writable && <button className="platform-small-button" onClick={() => activate({mode: "edit", id: detail.data!.id})}><Settings2 size={15} /> Edit mission</button>}
          {renderDocument(detail.data, close)}</>}
    </Modal>}
  </section>;
}
