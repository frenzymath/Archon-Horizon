import React, { useEffect, useMemo, useState } from 'react';
import { Link, Navigate, NavLink, Route, Routes, useSearchParams } from 'react-router-dom';
import { editInbox, editRoadmap, editTask, getState, getProjects, getReport, getTranscript, getTranscripts, searchDeclarations, getBlueprintChapters, type ProjectStat } from './api';
import { isStaticDashboard } from './staticMode';
import { version as APP_VERSION } from '../package.json';
import MarkdownBlock, { markdownToHtml } from './components/MarkdownBlock';
import BlueprintRendered from './components/BlueprintRendered';
import LeanCodeLine from './components/LeanCodeLine';
import BlueprintPage from './BlueprintPage';
import DagPage from './DagPage';
import LeanPage from './LeanPage';

const STATIC = isStaticDashboard();
// Triage labels (must match core/labels.py). The UI gate vocabulary stays
// accept/pending/reject; these are the on-disk label strings they map to.
const ARCHON_ACCEPT = 'agent-ready';
const ARCHON_PENDING = 'not-ready';
const ARCHON_REJECTED = 'rejected';
const INBOX_KIND_OPTIONS = ['hint', 'issue', 'protection', 'info', 'memory'];

type HorizonState = {
  workspace: string;
  projects?: string[];
  roadmap?: { items?: any[] };
  tasks?: any[];
  runs?: any[];
  local_inbox?: any[];
  github_inbox?: any[];
  memory?: string;
  reports?: string[];
  blueprints?: Record<string, any>;
  events?: any[];
  harnesses?: Record<string, any>;
  inbox_providers?: Record<string, { enabled: boolean; repo?: string | null; capabilities?: string[] }>;
};

type PageProps = {
  state: HorizonState;
  reload: () => void;
};

// Keep one crashing view from blanking the whole dashboard, and surface the
// error message (e.g. a vis-network DataSet throw) instead of a white screen.
class ErrorBoundary extends React.Component<{ children: React.ReactNode }, { error: Error | null }> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <div className="notice error">
            <strong>This view crashed.</strong>
            <pre style={{ whiteSpace: 'pre-wrap', marginTop: 8 }}>{this.state.error.message}</pre>
            <button onClick={() => this.setState({ error: null })}>Retry</button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function ConnectionBanner({ isError }: { isError: boolean }) {
  if (STATIC || !isError) return null;
  return (
    <div className="connection-banner">
      Cannot reach server - check that <code>horizon serve</code> is running on the current port.
    </div>
  );
}

export function App() {
  const [state, setState] = useState<HorizonState | null>(null);
  const [isError, setIsError] = useState(false);

  const reload = () => {
    getState()
      .then((next) => {
        setState(next);
        setIsError(false);
      })
      .catch(() => setIsError(true));
  };

  useEffect(() => {
    reload();
    if (STATIC) return;
    const id = setInterval(reload, 5000);
    return () => clearInterval(id);
  }, []);

  if (!state) {
    return (
      <div className="app">
        <ConnectionBanner isError={isError} />
        <main className="main-content loading">Loading dashboard...</main>
      </div>
    );
  }

  return (
    <div className="app">
      <ConnectionBanner isError={isError} />
      <header className="header">
        <h1>Archon Horizon</h1>
        <span className="version-badge" title={`Horizon dashboard v${APP_VERSION}`}>v{APP_VERSION}</span>
        <span className="project-badge" title={state.workspace}>{state.workspace}</span>
        {STATIC && <span className="project-badge" title={window.__ARCHON_STATIC__?.generatedAt}>static</span>}
        <nav className="header-nav" aria-label="Dashboard">
          <NavLink to="/" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`} end>Overview</NavLink>
          <NavLink to="/inbox" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Inbox</NavLink>
          <NavLink to="/roadmap" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Roadmap</NavLink>
          <NavLink to="/tasks" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Tasks</NavLink>
          <NavLink to="/search" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Search</NavLink>
          <NavLink to="/blueprint" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Blueprint</NavLink>
          <NavLink to="/dag" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>DAG</NavLink>
          <NavLink to="/logs" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Logs</NavLink>
          <NavLink to="/lean" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Lean</NavLink>
        </nav>
      </header>
      <main className="main-content">
        <ErrorBoundary>
        <Routes>
          <Route path="/" element={<Overview state={state} reload={reload} />} />
          <Route path="/inbox" element={<InboxPage state={state} reload={reload} />} />
          <Route path="/roadmap" element={<RoadmapPage state={state} reload={reload} />} />
          <Route path="/tasks" element={<TasksPage state={state} reload={reload} />} />
          <Route path="/search" element={<SearchPage state={state} />} />
          <Route path="/blueprint" element={<BlueprintPage state={state} />} />
          <Route path="/dag" element={<DagPage state={state} />} />
          <Route path="/logs" element={<Transcripts state={state} />} />
          <Route path="/transcripts" element={<Navigate to="/logs" replace />} />
          <Route path="/lean" element={<LeanPage state={state} />} />
          <Route path="/code" element={<Navigate to="/lean" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </ErrorBoundary>
      </main>
    </div>
  );
}

function Overview({ state }: PageProps) {
  const roadmapItems = state.roadmap?.items ?? [];
  const tasks = state.tasks ?? [];
  const runs = state.runs ?? [];
  const localInbox = state.local_inbox ?? [];
  const githubInbox = state.github_inbox ?? [];
  const activeRoadmap = roadmapItems.filter((item: any) => ['active', 'blocked'].includes(item.status));
  const activeTasks = tasks.filter((task: any) => ['queued', 'running', 'blocked'].includes(task.status));

  return (
    <div className="page page-narrow">
      <section className="hero-strip">
        <div>
          <p className="eyebrow">Workspace</p>
          <h2>{state.workspace}</h2>
        </div>
        <div className="hero-meta">
          <span>{STATIC ? 'Static export' : 'Live dashboard'}</span>
          <span>{new Date().toLocaleString()}</span>
        </div>
      </section>

      <ProjectsPanel />

      <Panel title="Recent Runs" subtitle="Run logs, rounds, sessions, and transcript links">
        <RunList runs={runs} />
      </Panel>

      <div className="overview-grid">
        <Panel title="Task Queue" subtitle="Active Horizon work items" to="/tasks">
          <TaskList tasks={(activeTasks.length ? activeTasks : tasks).slice(0, 6)} compact />
        </Panel>
        <Panel title="Roadmap Focus" subtitle="Active and blocked roadmap items" to="/roadmap">
          <RoadmapFocus items={(activeRoadmap.length ? activeRoadmap : roadmapItems).slice(0, 6)} compact />
        </Panel>
        <Panel title="Inbox" subtitle="Most recent open items" to="/inbox">
          <InboxSummary local={localInbox} github={githubInbox} />
        </Panel>
      </div>
    </div>
  );
}

function ProjectsPanel() {
  const [data, setData] = useState<{ projects: ProjectStat[]; totals: Omit<ProjectStat, 'name'> } | null>(null);
  useEffect(() => { getProjects().then(setData).catch(() => setData(null)); }, []);
  if (!data || data.projects.length === 0) return null;
  const fmt = (n: number) => n.toLocaleString();
  return (
    <Panel title="Workspace" subtitle="Per-project Lean size, open sorries, and blueprint coverage">
      <div className="table-wrap">
        <table className="projects-table">
          <thead>
            <tr>
              <th>Project</th><th>Lean files</th><th>LOC</th><th>Code</th><th>Open sorries</th><th>Blueprint proved</th>
            </tr>
          </thead>
          <tbody>
            {data.projects.map((p) => (
              <tr key={p.name}>
                <td>
                  <strong>{p.name}</strong>
                  {(p.depends_on ?? []).length > 0 && (
                    <div className="project-deps">
                      depends on {(p.depends_on ?? []).join(', ')}
                    </div>
                  )}
                </td>
                <td>{fmt(p.lean_files)}</td>
                <td>{fmt(p.loc)}</td>
                <td>{fmt(p.loc_code)}</td>
                <td>{p.sorries > 0 ? <span className="sorry-pill">{p.sorries}</span> : <span className="ok-pill">0</span>}</td>
                <td><BlueprintProgress ok={p.blueprint_leanok} total={p.blueprint_nodes} /></td>
              </tr>
            ))}
            <tr className="projects-total">
              <td>Total</td>
              <td>{fmt(data.totals.lean_files)}</td>
              <td>{fmt(data.totals.loc)}</td>
              <td>{fmt(data.totals.loc_code)}</td>
              <td>{data.totals.sorries > 0 ? <span className="sorry-pill">{data.totals.sorries}</span> : <span className="ok-pill">0</span>}</td>
              <td><BlueprintProgress ok={data.totals.blueprint_leanok} total={data.totals.blueprint_nodes} /></td>
            </tr>
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

function BlueprintProgress({ ok, total }: { ok: number; total: number }) {
  if (!total) return <span className="empty">No blueprint nodes</span>;
  const pct = Math.round((ok / total) * 100);
  return (
    <div className="blueprint-progress" title={`${ok} proved or Lean-linked nodes out of ${total}`}>
      <span className="blueprint-progress-text">{pct}% <span>{ok} of {total}</span></span>
    </div>
  );
}

// Shared status → colour for roadmap + task chips.
function statusColor(status: string) {
  switch (status) {
    case 'active': case 'running': return 'var(--blue)';
    case 'pending': case 'queued': return 'var(--orange)';
    case 'blocked': return 'var(--red)';
    case 'done': return 'var(--green)';
    case 'failed': case 'rejected': case 'cancelled': return 'var(--red)';
    default: return 'var(--text-muted)';
  }
}

// Pick one or more projects: a checkbox per known project, plus free-text add for
// projects not in the list. Used by the task + roadmap composers/editors.
function MultiProjectSelect({ value, onChange, projects }: { value: string[]; onChange: (v: string[]) => void; projects: string[] }) {
  const toggle = (p: string) => onChange(value.includes(p) ? value.filter((x) => x !== p) : [...value, p]);
  const known = new Set(projects);
  const extras = value.filter((v) => !known.has(v));
  return (
    <div className="project-multiselect">
      {[...projects, ...extras].map((p) => (
        <label key={p} className={`project-chip ${value.includes(p) ? 'on' : ''}`}>
          <input type="checkbox" checked={value.includes(p)} onChange={() => toggle(p)} />
          {p}
        </label>
      ))}
      <input
        className="project-add"
        placeholder="+ project"
        onKeyDown={(e) => {
          if (e.key !== 'Enter') return;
          e.preventDefault();
          const v = e.currentTarget.value.trim();
          if (v && !value.includes(v)) onChange([...value, v]);
          e.currentTarget.value = '';
        }}
      />
    </div>
  );
}

function TasksPage({ state, reload }: PageProps) {
  const tasks = state.tasks ?? [];
  const roadmapIds = (state.roadmap?.items ?? []).map((item: any) => item.id);
  const projects = state.projects ?? [];
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);

  const runAction = (payload: Record<string, unknown>, success?: string) => {
    setMessage(null);
    return editTask(payload)
      .then((result) => {
        if (result?.ok === false) throw new Error('operation failed');
        if (success) setMessage({ kind: 'info', text: success });
        reload();
        return true;
      })
      .catch((error) => {
        setMessage({ kind: 'error', text: error.message || String(error) });
        return false;
      });
  };

  return (
    <div className="page">
      <Panel title="Tasks" subtitle={`${tasks.length} task${tasks.length === 1 ? '' : 's'}`}>
        {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
        {!STATIC && (
          <details className="local-create">
            <summary>New task</summary>
            <TaskComposer runAction={runAction} projects={projects} roadmapIds={roadmapIds} />
          </details>
        )}
        <div className="task-list">
          {tasks.map((task: any) => (
            <TaskCard key={task.id} task={task} runAction={runAction} projects={projects} roadmapIds={roadmapIds} />
          ))}
          {tasks.length === 0 && <p className="empty">No tasks.</p>}
        </div>
      </Panel>
    </div>
  );
}

function splitList(value: string) {
  return value.split(',').map((part) => part.trim()).filter(Boolean);
}

function TaskComposer({ runAction, projects, roadmapIds }: { runAction: any; projects: string[]; roadmapIds: string[] }) {
  const [name, setName] = useState('');
  const [title, setTitle] = useState('');
  const [explanation, setExplanation] = useState('');
  const [selProjects, setSelProjects] = useState<string[]>(projects[0] ? [projects[0]] : []);
  const [priority, setPriority] = useState('normal');
  const [author, setAuthor] = useState('');
  const [roadmapRefs, setRoadmapRefs] = useState('');
  const [files, setFiles] = useState('');

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!title.trim() || selProjects.length === 0 || !author.trim()) return;
    runAction({
      action: 'add',
      id: name.trim() || title.trim(),
      title: title.trim(),
      explanation: explanation.trim(),
      projects: selProjects,
      priority,
      author: author.trim(),
      roadmap_refs: splitList(roadmapRefs),
      files: splitList(files),
    }, 'Task added').then((ok: boolean) => {
      if (!ok) return;
      setName('');
      setTitle('');
      setExplanation('');
      setRoadmapRefs('');
      setFiles('');
    });
  };

  return (
    <form className="composer" onSubmit={submit}>
      <datalist id="task-roadmap-refs">{roadmapIds.map((id) => <option key={id} value={id} />)}</datalist>
      <div className="composer-row">
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="task-name (optional)" />
        <input value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="Author (required)" />
        <select value={priority} onChange={(e) => setPriority(e.target.value)}>
          <option value="urgent">urgent</option>
          <option value="high">high</option>
          <option value="normal">normal</option>
          <option value="low">low</option>
        </select>
      </div>
      <label className="composer-label">Projects</label>
      <MultiProjectSelect value={selProjects} onChange={setSelProjects} projects={projects} />
      <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" />
      <textarea value={explanation} onChange={(e) => setExplanation(e.target.value)} rows={4} placeholder="Explanation (Markdown / LaTeX supported)" />
      <div className="composer-row">
        <input value={roadmapRefs} onChange={(e) => setRoadmapRefs(e.target.value)} placeholder="Roadmap refs, comma-separated" list="task-roadmap-refs" />
        <input value={files} onChange={(e) => setFiles(e.target.value)} placeholder="Writable files, comma-separated" />
      </div>
      <button className="primary" type="submit" disabled={!title.trim() || selProjects.length === 0 || !author.trim()}>Add task</button>
    </form>
  );
}

function TaskCard({ task, runAction, projects = [], roadmapIds = [] }: { task: any; runAction?: any; projects?: string[]; roadmapIds?: string[] }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(task.title || task.objective || task.id);
  const [explanation, setExplanation] = useState(task.explanation || task.objective || '');
  const [selProjects, setSelProjects] = useState<string[]>(task.projects ?? task.write_set?.projects ?? (task.project ? [task.project] : []));
  const [priority, setPriority] = useState(task.priority || 'normal');
  const [author, setAuthor] = useState(task.metadata?.author || '');
  const [status, setStatus] = useState(task.status || 'queued');
  const [roadmapRefs, setRoadmapRefs] = useState((task.roadmap_refs ?? []).join(', '));
  const [files, setFiles] = useState((task.write_set?.files ?? []).join(', '));

  const taskProjects: string[] = task.projects ?? task.write_set?.projects ?? (task.project ? [task.project] : []);
  const writeFiles: string[] = task.write_set?.files ?? [];
  const comments: any[] = task.metadata?.comments ?? [];
  const history = historyOf(task);
  const descriptionBody: string = task.explanation || task.objective || '';
  const createdAt = task.created_at ?? task.metadata?.created_at;
  const updatedAt = task.updated_at ?? task.metadata?.updated_at;

  const updateStatus = (next: string) => {
    const previous = status;
    setStatus(next);
    runAction?.({ action: 'status', id: task.id, status: next }).then((ok: boolean) => {
      if (!ok) setStatus(previous);
    });
  };
  const updatePriority = (next: string) => {
    const previous = priority;
    setPriority(next);
    runAction?.({ action: 'edit', id: task.id, priority: next }).then((ok: boolean) => {
      if (!ok) setPriority(previous);
    });
  };
  // Re-sync inline-editable fields when fresh props arrive after a reload.
  useEffect(() => {
    setStatus(task.status || 'queued');
    setPriority(task.priority || 'normal');
  }, [task.status, task.priority]);
  const save = (e: React.FormEvent) => {
    e.preventDefault();
    if (!author.trim()) return;
    runAction?.({
      action: 'edit',
      id: task.id,
      title: title.trim(),
      explanation: explanation.trim(),
      projects: selProjects,
      priority,
      author: author.trim(),
      roadmap_refs: splitList(roadmapRefs),
      files: splitList(files),
    }).then((ok: boolean) => ok && setEditing(false));
  };
  const deleteTask = () => {
    if (window.confirm(`Delete task ${task.id}?`)) runAction?.({ action: 'delete', id: task.id });
  };

  return (
    <details className="task-card" open={editing}>
      <summary style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div className="task-title" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: 1, minWidth: 0 }}>
          {!STATIC && runAction ? (
            <select className="chip-select state" style={{ color: statusColor(status), borderColor: statusColor(status) }} value={status} onChange={(e) => updateStatus(e.target.value)}>
              <option value="queued">queued</option>
              <option value="running">running</option>
              <option value="blocked">blocked</option>
              <option value="done">done</option>
              <option value="failed">failed</option>
              <option value="cancelled">cancelled</option>
            </select>
          ) : <Status value={task.status} />}
          <strong className="clamp-1"><InlineMarkdown content={task.title || task.objective || task.id} /></strong>
          {!STATIC && runAction ? (
            <select className={`chip-select priority priority-${priority}`} value={priority} onChange={(e) => updatePriority(e.target.value)} aria-label="Priority">
              <option value="urgent">urgent</option>
              <option value="high">high</option>
              <option value="normal">normal</option>
              <option value="low">low</option>
            </select>
          ) : <span className={`priority-chip priority-${priority}`}>{priority}</span>}
          {task.metadata?.author && <span className="author-tag">by {task.metadata.author}</span>}
          <ProvenanceChip provenance={task.metadata?.provenance} />
          {(task.updated_at || task.metadata?.updated_at) && <span className="author-tag" style={{ marginLeft: 'auto' }}>updated {new Date(task.updated_at || task.metadata.updated_at).toLocaleDateString()}</span>}
        </div>
        {!STATIC && runAction && <button className="icon-danger button-reset" onClick={deleteTask} title="Delete task" aria-label="Delete task"><TrashIcon /></button>}
      </summary>
      <div className="task-body">
        {editing ? (
          <form className="inline-edit" onSubmit={save}>
            <datalist id={`task-roadmap-${task.id}`}>{roadmapIds.map((id) => <option key={id} value={id} />)}</datalist>
            <label className="composer-label">Projects</label>
            <MultiProjectSelect value={selProjects} onChange={setSelProjects} projects={projects} />
            <div className="composer-row">
              <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Title" />
              <input value={author} onChange={(e) => setAuthor(e.target.value)} placeholder="Author (required)" />
              <select value={priority} onChange={(e) => setPriority(e.target.value)}>
                <option value="urgent">urgent</option>
                <option value="high">high</option>
                <option value="normal">normal</option>
                <option value="low">low</option>
              </select>
            </div>
            <textarea value={explanation} onChange={(e) => setExplanation(e.target.value)} rows={4} placeholder="Explanation (Markdown)" />
            <div className="composer-row">
              <input value={roadmapRefs} onChange={(e) => setRoadmapRefs(e.target.value)} placeholder="Roadmap refs" list={`task-roadmap-${task.id}`} />
              <input value={files} onChange={(e) => setFiles(e.target.value)} placeholder="Writable files" />
            </div>
            <div className="inline-edit-actions">
              <button type="submit" disabled={!author.trim()}>Save</button>
              <button type="button" onClick={() => setEditing(false)}>Cancel</button>
            </div>
          </form>
        ) : (
          <div className="task-detail">
            <div className="item-meta">
              <span>{task.id}</span>
              {task.metadata?.author && <span>by {task.metadata.author}</span>}
              <ProvenanceChip provenance={task.metadata?.provenance} />
              {createdAt && <span>created {formatDate(createdAt)}</span>}
              {updatedAt && <span>updated {formatDate(updatedAt)}</span>}
            </div>
            <div className="roadmap-fields">
              <FieldChips label="Projects" values={taskProjects} />
              <FieldChips label="Roadmap" values={task.roadmap_refs ?? []} />
              <FieldChips label="Writable" values={writeFiles} />
            </div>
            <ActivityTimeline
              description={descriptionBody ? { body: descriptionBody, author: task.metadata?.author, at: createdAt } : undefined}
              comments={comments}
              history={history}
              editable={false}
              itemId={task.id}
              runAction={runAction}
            />
            {!descriptionBody && comments.length === 0 && history.length === 0 && <p className="empty">No description.</p>}
            {!STATIC && runAction && <CommentComposer id={task.id} runAction={runAction} />}
            {!STATIC && runAction && <button className="text-action" onClick={() => setEditing(true)}>Modify</button>}
          </div>
        )}
      </div>
    </details>
  );
}

// Lean declaration kinds → the same colour family the blueprint/DAG use.
const SEARCH_KINDS = ['theorem', 'lemma', 'def', 'abbrev', 'structure', 'inductive', 'class', 'instance', 'axiom'];
function kindColorVar(kind: string): string {
  switch (kind) {
    case 'theorem': return 'var(--kind-theorem)';
    case 'lemma': return 'var(--kind-lemma)';
    case 'def': case 'abbrev': case 'opaque': case 'structure': case 'inductive': case 'class': case 'instance':
      return 'var(--kind-definition)';
    case 'axiom': return 'var(--kind-conjecture)';
    case 'blueprint': return 'var(--kind-remark)';
    default: return 'var(--kind-neutral)';
  }
}

function SearchResultRow({ hit, macros, ensureMacros }: { hit: any; macros: Record<string, any>; ensureMacros: (project: string) => void }) {
  const [open, setOpen] = useState(false);
  const bp = hit.blueprint;
  const color = kindColorVar(hit.kind);
  const sigLines: string[] = String(hit.signature || '').split('\n').filter((l) => l.length);

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && bp?.project) ensureMacros(bp.project);
  };

  return (
    <div className={`search-row${open ? ' open' : ''}`} style={{ borderLeftColor: color }}>
      <button type="button" className="search-row-head" onClick={toggle}>
        <span className="search-kind" style={{ color, borderColor: color }}>{hit.kind}</span>
        <code className="search-name clamp-1">{hit.name || bp?.id || '(unnamed)'}</code>
        {bp && <span className="search-haslatex" title="Has a blueprint statement">LaTeX</span>}
        {bp?.leanok && <span className="status status-done" title="Formalised in Lean">✓</span>}
        <span className="search-lib">{hit.library}</span>
        <span className="ev-caret">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="search-row-body">
          <div className="search-lean">
            {hit.name ? (
              <>
                {sigLines.length > 0 && (
                  <pre className="search-sig lean-code">
                    {sigLines.map((line, i) => <div key={i} className="search-sig-line"><LeanCodeLine text={line} /></div>)}
                  </pre>
                )}
                {hit.doc && <div className="search-doc"><MarkdownBlock content={hit.doc} /></div>}
                {hit.file && <div className="search-loc">{hit.file}{hit.line ? `:${hit.line}` : ''}</div>}
              </>
            ) : <span className="empty">Not formalised in Lean yet.</span>}
          </div>
          <div className="search-latex">
            {bp ? (
              <>
                {(bp.title || bp.id) && <div className="search-bp-title">{bp.title || bp.id}{bp.title && <span className="badge">{bp.id}</span>}</div>}
                {/* Render the node's statement exactly as the blueprint/DAG card does
                    — pass the raw statement (no env wrapper, which would force a
                    wrong "Theorem" label) plus the project's macros. */}
                <div className="latex-rendered"><BlueprintRendered tex={bp.statement || ''} macros={macros[bp.project] || {}} /></div>
              </>
            ) : <span className="empty">No linked blueprint statement.</span>}
          </div>
        </div>
      )}
    </div>
  );
}

function ChipMultiSelect({ options, selected, onToggle, label }: { options: string[]; selected: Set<string>; onToggle: (v: string) => void; label: string }) {
  if (!options.length) return null;
  return (
    <div className="search-facet" aria-label={label}>
      {options.map((opt) => (
        <button key={opt} type="button" className={`search-facet-chip ${selected.has(opt) ? 'on' : ''}`} onClick={() => onToggle(opt)}>{opt}</button>
      ))}
    </div>
  );
}

function SearchPage({ state }: { state?: any }) {
  const libraries: string[] = state?.libraries ?? state?.projects ?? [];
  const [q, setQ] = useState('');
  const [mode, setMode] = useState('text');
  const [libs, setLibs] = useState<Set<string>>(new Set());
  const [kinds, setKinds] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(10);
  const [results, setResults] = useState<any[] | null>(null);
  const [info, setInfo] = useState('');
  const [loading, setLoading] = useState(false);
  const [macros, setMacros] = useState<Record<string, any>>({});

  // Fetch (and cache) a project's blueprint macros so its statements render with
  // the right custom commands — the same data the Blueprint page uses.
  const ensureMacros = (project: string) => {
    if (!project || macros[project]) return;
    setMacros((m) => ({ ...m, [project]: {} }));
    getBlueprintChapters(project).then((data) => setMacros((m) => ({ ...m, [project]: data.macros ?? {} }))).catch(() => {});
  };

  // Functional update so selecting several facets accumulates correctly even if
  // two clicks land before a re-render (a plain `new Set(libs)` could read a
  // stale value and keep only the last selection).
  const toggleIn = (setter: React.Dispatch<React.SetStateAction<Set<string>>>) => (v: string) =>
    setter((prev) => {
      const next = new Set(prev);
      next.has(v) ? next.delete(v) : next.add(v);
      return next;
    });
  const toggleLib = toggleIn(setLibs);
  const toggleKind = toggleIn(setKinds);

  // Debounced live search: the index/query run on the server, not in the SPA.
  useEffect(() => {
    if (STATIC) return;
    const query = q.trim();
    if (!query) { setResults(null); setInfo(''); return; }
    setLoading(true);
    const handle = setTimeout(() => {
      searchDeclarations(query, mode, [...libs], [...kinds], limit)
        .then((data) => {
          setResults(data.results ?? []);
          setInfo(`${data.count} result${data.count === 1 ? '' : 's'} · ${(data.indexed ?? 0).toLocaleString()} declarations indexed`);
        })
        .catch((e) => { setResults([]); setInfo(e?.message || String(e)); })
        .finally(() => setLoading(false));
    }, 250);
    return () => clearTimeout(handle);
  }, [q, mode, libs, kinds, limit]);

  const placeholder = mode === 'type'
    ? 'Type pattern — ?a → ?a → Prop'
    : mode === 'name'
    ? 'Name fragment — Continuous.comp'
    : 'Informal query — commutativity of addition';

  return (
    <div className="page">
      <Panel title="Search" subtitle="Informal = keywords over Lean names/signatures/docstrings + blueprint LaTeX (ranked by how many words match); Name / Type for precise lookups">
        {STATIC && <div className="notice info">Search needs the live dashboard — it queries the workspace index. Run <code>horizon dashboard</code> (without <code>--static</code>).</div>}
        <div className="search-controls">
          <input className="search-input" autoFocus value={q} placeholder={placeholder} onChange={(e) => setQ(e.target.value)} />
          <div className="search-modes">
            {([['text', 'Informal'], ['name', 'Name'], ['type', 'Type']] as const).map(([m, label]) => (
              <button key={m} type="button" className={`search-mode ${mode === m ? 'on' : ''}`} onClick={() => setMode(m)}>{label}</button>
            ))}
          </div>
          <select value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Number of results" title="How many results to show">
            {[5, 10, 25, 50].map((n) => <option key={n} value={n}>{n} results</option>)}
          </select>
        </div>
        <div className="search-facets">
          {libraries.length > 1 && (
            <div className="search-facet-group"><span className="search-facet-label">Libraries</span>
              <ChipMultiSelect options={libraries} selected={libs} onToggle={toggleLib} label="Libraries" />
            </div>
          )}
          <div className="search-facet-group"><span className="search-facet-label">Kinds</span>
            <ChipMultiSelect options={SEARCH_KINDS} selected={kinds} onToggle={toggleKind} label="Kinds" />
          </div>
        </div>
        {info && <p className="search-info">{loading ? 'Searching…' : info}</p>}
        <div className="search-results">
          {results?.map((r, i) => <SearchResultRow key={`${r.name ?? r.blueprint?.id ?? 'x'}-${i}`} hit={r} macros={macros} ensureMacros={ensureMacros} />)}
          {results && results.length === 0 && !loading && <p className="empty">No matches.</p>}
          {results === null && !STATIC && <p className="empty">Type a query to search the Lean declarations and blueprint statements.</p>}
        </div>
      </Panel>
    </div>
  );
}

export function Panel({ title, subtitle, to, children }: { title: string; subtitle?: string; to?: string; children: React.ReactNode }) {
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {to && <Link className="panel-link" to={to}>View all →</Link>}
      </div>
      {children}
    </section>
  );
}

function RoadmapPage({ state, reload }: PageProps) {
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const items = state.roadmap?.items ?? [];
  const allProjects = state.projects || [];

  const [projectFilter, setProjectFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');

  const runAction = (payload: Record<string, unknown>, success?: string) => {
    setMessage(null);
    return editRoadmap(payload)
      .then((result) => {
        if (result?.ok === false) {
          throw new Error('operation failed');
        }
        if (success) setMessage({ kind: 'info', text: success });
        reload();
        return true;
      })
      .catch((error) => {
        setMessage({ kind: 'error', text: error.message || String(error) });
        return false;
      });
  };

  const itemProjects = (i: any): string[] => (i.projects?.length ? i.projects : ['Uncategorized']);
  const filteredItems = items.filter((i: any) => {
    if (projectFilter !== 'all' && !itemProjects(i).includes(projectFilter)) return false;
    if (statusFilter !== 'all' && i.status !== statusFilter) return false;
    return true;
  });
  // An item shared across projects appears under each of its project groups.
  const filteredProjects = [...new Set(filteredItems.flatMap(itemProjects))].sort();

  return (
    <div className="page">
      <Panel title="Roadmap" subtitle={`${filteredItems.length} item${filteredItems.length === 1 ? '' : 's'}`}>
        {!STATIC && (
          <details className="local-create">
            <summary>New roadmap item</summary>
            <RoadmapComposer runAction={runAction} projects={allProjects} />
          </details>
        )}
        <div className="filters">
          <select value={projectFilter} onChange={(e) => setProjectFilter(e.target.value)}>
            <option value="all">All projects</option>
            {allProjects.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="pending">Pending</option>
            <option value="blocked">Blocked</option>
            <option value="done">Done</option>
            <option value="rejected">Rejected</option>
          </select>
        </div>
        {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
        <div className="roadmap-list" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {filteredProjects.map((proj) => (
            <div key={proj as string} className="roadmap-project-group">
              <h3>{proj}</h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {filteredItems.filter((i: any) => itemProjects(i).includes(proj)).map((item: any) => (
                  <RoadmapItemCard key={item.id} item={item} runAction={runAction} projects={allProjects} />
                ))}
              </div>
            </div>
          ))}
          {filteredItems.length === 0 && <p className="empty">No roadmap items.</p>}
        </div>
      </Panel>
    </div>
  );
}

function RoadmapComposer({ runAction, projects }: { runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>, projects: string[] }) {
  const [title, setTitle] = useState('');
  const [summary, setSummary] = useState('');
  const [selProjects, setSelProjects] = useState<string[]>([]);
  const [author, setAuthor] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!title.trim() || !author.trim() || busy) return;
    setBusy(true);
    runAction({
      action: 'add',
      title: title.trim(),
      summary: summary.trim(),
      projects: selProjects,
      author: author.trim()
    }, 'Roadmap item added.')
      .then((ok) => {
        if (!ok) return;
        setTitle('');
        setSummary('');
        setAuthor('');
        setSelProjects([]);
      })
      .finally(() => setBusy(false));
  };

  return (
    <form className="composer" onSubmit={submit}>
      <label className="composer-label">Projects <span className="composer-hint">(pick one or more — shared items can span several)</span></label>
      <MultiProjectSelect value={selProjects} onChange={setSelProjects} projects={projects} />
      <input value={author} placeholder="Author (required)" onChange={(event) => setAuthor(event.target.value)} />
      <input value={title} placeholder="Title" onChange={(event) => setTitle(event.target.value)} />
      <textarea value={summary} placeholder="Summary (Markdown / LaTeX supported)" onChange={(event) => setSummary(event.target.value)} rows={3} />
      <button className="primary" type="submit" disabled={!title.trim() || !author.trim() || busy}>Add roadmap item</button>
    </form>
  );
}


function InboxPage({ state, reload }: PageProps) {
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const [query, setQuery] = useState('');
  const [providerFilter, setProviderFilter] = useState('all');
  const [statusFilter, setStatusFilter] = useState('all');
  const [gateFilter, setGateFilter] = useState('all');
  const providers = state.inbox_providers ?? {};
  const github = providers.github;
  const allItems = [...(state.local_inbox ?? []), ...(state.github_inbox ?? [])];
  const items = allItems
    .filter((item) => matchesInboxFilters(item, { query, providerFilter, statusFilter, gateFilter }))
    .sort(compareInboxItems);

  const runAction = (payload: Record<string, unknown>, success?: string) => {
    setMessage(null);
    return editInbox(payload)
      .then((result) => {
        if (result?.ok === false) {
          const errors = (result.errors ?? []).join('; ') || 'operation failed';
          throw new Error(errors);
        }
        if (success) setMessage({ kind: 'info', text: success });
        reload();
        return true;
      })
      .catch((error) => {
        setMessage({ kind: 'error', text: error.message || String(error) });
        return false;
      });
  };

  return (
    <div className="page">
      <Panel title="Inbox" subtitle="Search, review, and close inbox items">
        <div className="inbox-topbar">
          <div className="inbox-searchbar">
            <input value={query} placeholder="Search inbox..." onChange={(event) => setQuery(event.target.value)} />
            <select value={providerFilter} onChange={(event) => setProviderFilter(event.target.value)} aria-label="Provider filter">
              <option value="all">All sources</option>
              <option value="local">Local</option>
              <option value="github">GitHub</option>
            </select>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="Status filter">
              <option value="all">All states</option>
              <option value="open">Open</option>
              <option value="completed">Closed</option>
              <option value="archived">Archived</option>
            </select>
            <select value={gateFilter} onChange={(event) => setGateFilter(event.target.value)} aria-label="Agent label filter">
              <option value="all">All labels</option>
              <option value="accept">Agent-ready</option>
              <option value="pending">Not-ready</option>
              <option value="reject">Rejected</option>
              <option value="clear">Unlabeled</option>
            </select>
          </div>
          <button
            onClick={() => runAction({ action: 'sync', provider: 'github' }, 'GitHub inbox synced.')}
            disabled={STATIC || !github?.enabled}
            title={github?.enabled ? 'Pull current issues and PRs through gh.' : 'Enable github in config.yaml to sync GitHub items.'}
          >
            Sync GitHub
          </button>
        </div>
        <details className="local-create">
          <summary>New local item</summary>
          <InboxComposer runAction={runAction} />
        </details>
        {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
        {STATIC && <div className="notice info">Static exports are read-only snapshots. Use the live dashboard for inbox actions.</div>}
        {!STATIC && github?.enabled && (
          <div className="notice info">
            GitHub actions require <code>gh</code> authentication and repository permission.
          </div>
        )}
        <div className="inbox-list">
          {items.map((item) => (
            <InboxCard
              key={`${item.provider}-${item.id}`}
              item={item}
              providers={providers}
              runAction={runAction}
            />
          ))}
          {items.length === 0 && <p className="empty inbox-empty">No inbox items match the current filters.</p>}
        </div>
      </Panel>
    </div>
  );
}

function InboxComposer({ runAction }: { runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean> }) {
  const [kind, setKind] = useState('hint');
  const [author, setAuthor] = useState('');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  if (STATIC) return null;

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const cleanAuthor = author.trim();
    const cleanTitle = title.trim();
    const cleanDescription = description.trim();
    if (!cleanAuthor || !cleanTitle || !cleanDescription || busy) return;
    setBusy(true);
    runAction({
      action: 'add',
      kind,
      author: cleanAuthor,
      title: cleanTitle,
      comment: cleanDescription,
    }, 'Local inbox item added.')
      .then((ok) => {
        if (!ok) return;
        setAuthor('');
        setTitle('');
        setDescription('');
      })
      .finally(() => setBusy(false));
  };

  return (
    <form className="composer" onSubmit={submit}>
      <div className="composer-row">
        <select value={kind} onChange={(event) => setKind(event.target.value)} aria-label="Inbox type">
          {INBOX_KIND_OPTIONS.map((option) => <option key={option} value={option}>{option}</option>)}
        </select>
        <input value={author} placeholder="Who is creating this item?" onChange={(event) => setAuthor(event.target.value)} />
      </div>
      <input value={title} placeholder="Title" onChange={(event) => setTitle(event.target.value)} />
      <textarea value={description} placeholder="Description" onChange={(event) => setDescription(event.target.value)} rows={3} />
      <button className="primary" type="submit" disabled={!author.trim() || !title.trim() || !description.trim() || busy}>Add local item</button>
    </form>
  );
}

function InboxCard({
  item,
  providers,
  runAction,
}: {
  item: any;
  providers: HorizonState['inbox_providers'];
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
}) {
  const [expanded, setExpanded] = useState(false);
  const provider = providers?.[item.provider] ?? { enabled: item.provider === 'local', capabilities: [] };
  const caps = new Set(provider.capabilities ?? []);
  const editable = !STATIC;
  const isGithub = item.provider === 'github';
  const sourceUrl = inboxSourceUrl(item, provider.repo ?? undefined);
  const comments = inboxComments(item);
  const gate = inboxGate(item.labels ?? []);
  const { title, body } = inboxTitleAndBody(item);
  const itemNumber = item.metadata?.number ? `#${item.metadata.number}` : item.id;
  const author = inboxAuthor(item);
  const itemStatus = (item.status === 'archived' || item.status === 'closed') ? 'completed' : item.status;
  const [visibleKind, setVisibleKind] = useState(item.kind);
  const [visibleStatus, setVisibleStatus] = useState(itemStatus);
  const [visibleGate, setVisibleGate] = useState(gate);

  useEffect(() => {
    setVisibleKind(item.kind);
    setVisibleStatus(itemStatus);
    setVisibleGate(gate);
  }, [item.id, item.kind, itemStatus, gate]);

  const pseudoProvider = author?.toLowerCase() === 'ground' ? 'ground' : author?.toLowerCase() === 'horizon' ? 'horizon' : item.provider;

  const deleteLocal = () => {
    if (!window.confirm(`Delete local inbox item ${item.id}? This cannot be undone.`)) return;
    runAction({ action: 'delete', provider: 'local', id: item.id }, 'Local inbox item deleted.');
  };

  const updateKind = (nextKind: string) => {
    const previous = visibleKind;
    setVisibleKind(nextKind);
    runAction({
      action: 'kind',
      provider: 'local',
      id: item.id,
      kind: nextKind,
    }, 'Inbox type updated.').then((ok) => {
      if (!ok) setVisibleKind(previous);
    });
  };

  const updateStatus = (nextStatus: string) => {
    const previous = visibleStatus;
    setVisibleStatus(nextStatus);
    runAction({
      action: nextStatus === 'open' ? 'reopen' : 'complete',
      provider: item.provider,
      id: item.id,
    }, nextStatus === 'open' ? 'Inbox item reopened.' : 'Inbox item closed.').then((ok) => {
      if (!ok) setVisibleStatus(previous);
    });
  };

  const updateGate = (nextGate: string) => {
    const previous = visibleGate;
    setVisibleGate(nextGate);
    runAction({
      action: 'gate',
      provider: item.provider,
      id: item.id,
      gate: nextGate,
    }, 'Inbox label updated.').then((ok) => {
      if (!ok) setVisibleGate(previous);
    });
  };

  const stop = (event: React.MouseEvent) => event.stopPropagation();

  return (
    <article className={`inbox-item source-${pseudoProvider}${expanded ? ' expanded' : ''}`}>
      <div className="inbox-item-content">
        <div className="issue-title-line" onClick={() => setExpanded((value) => !value)}>
          <div className="issue-title-main">
            <SourceMark provider={pseudoProvider} />
            <span className={`source-chip ${item.provider}`}>{item.provider}</span>
            {editable && item.provider === 'local' ? (
              <select className={`chip-select type ${visibleKind}`} value={visibleKind} onClick={stop} onChange={(event) => updateKind(event.target.value)} aria-label="Type">
                {INBOX_KIND_OPTIONS.map((option) => (
                  <option className={`kind-${option}`} key={option} value={option}>{option}</option>
                ))}
              </select>
            ) : <span className={`type-chip type-${item.kind}`}>{item.kind}</span>}
            {editable && caps.has('status') ? (
              <select className={`chip-select state ${visibleStatus}`} value={visibleStatus} onClick={stop} onChange={(event) => updateStatus(event.target.value)} aria-label="State">
                <option className="state-open" value="open">open</option>
                <option className="state-completed" value="completed">closed</option>
              </select>
            ) : <Status value={item.status} />}
            {editable && caps.has('label') ? (
              <select className={`chip-select gate ${visibleGate}`} value={visibleGate} onClick={stop} onChange={(event) => updateGate(event.target.value)} aria-label="Agent label">
                <option className="gate-accept" value="accept">agent-ready</option>
                <option className="gate-pending" value="pending">not-ready</option>
                <option className="gate-reject" value="reject">rejected</option>
                <option className="gate-clear" value="clear">unlabeled</option>
              </select>
            ) : <Status value={gate} label={gateLabel(gate)} />}
            {sourceUrl ? (
              <a className="issue-title" href={sourceUrl} target="_blank" rel="noreferrer" onClick={stop}><InlineMarkdown content={title} /></a>
            ) : (
              <span className="issue-title"><InlineMarkdown content={title} /></span>
            )}
            {author && <span className="author-tag">by {author}</span>}
          </div>
          <div className="issue-title-actions">
            {comments.length > 0 && (
              <span className="comment-count" title={`${comments.length} comment${comments.length === 1 ? '' : 's'}`}>{comments.length}</span>
            )}
            {editable && item.provider === 'local' && (
              <button className="icon-danger" onClick={(event) => { stop(event); deleteLocal(); }} title="Delete local item" aria-label="Delete local item"><TrashIcon /></button>
            )}
            <span className="ev-caret">{expanded ? '▾' : '▸'}</span>
          </div>
        </div>
        {expanded && (
          <div className="issue-body">
            <div className="issue-meta">
              <span>{itemNumber}</span>
              {author && <span>by {author}</span>}
              {item.audience && <span className="audience-chip" title="Who this item is addressed to">to {item.audience}</span>}
              <ProvenanceChip provenance={item.metadata?.provenance} />
              <span>opened {formatDate(item.created_at)}</span>
              {item.updated_at && item.updated_at !== item.created_at && <span>updated {formatDate(item.updated_at)}</span>}
              {sourceUrl && <a href={sourceUrl} target="_blank" rel="noreferrer">Open on GitHub</a>}
            </div>
            <EditableDescription
              body={body}
              createdAt={item.created_at}
              editable={editable && item.provider === 'local'}
              itemId={item.id}
              runAction={runAction}
              title={title}
              titleLabel={author ?? (isGithub ? 'GitHub description' : 'Description')}
            />
            <ActivityTimeline
              comments={comments}
              history={historyOf(item)}
              editable={editable && item.provider === 'local'}
              itemId={item.id}
              runAction={runAction}
            />
            {editable && caps.has('comment') && <CommentBox item={item} runAction={runAction} />}
          </div>
        )}
      </div>
    </article>
  );
}

function EditableDescription({
  body,
  createdAt,
  editable,
  itemId,
  runAction,
  title,
  titleLabel,
}: {
  body: string;
  createdAt: string;
  editable: boolean;
  itemId: string;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
  title: string;
  titleLabel: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(title);
  const [draftBody, setDraftBody] = useState(body);

  useEffect(() => {
    setDraftTitle(title);
    setDraftBody(body);
  }, [title, body]);

  if (!body && !editable) return null;

  const cancel = () => {
    setDraftTitle(title);
    setDraftBody(body);
    setEditing(false);
  };
  const save = (event: React.FormEvent) => {
    event.preventDefault();
    const cleanTitle = draftTitle.trim();
    const cleanBody = draftBody.trim();
    if (!cleanTitle || !cleanBody) return;
    runAction({
      action: 'body',
      provider: 'local',
      id: itemId,
      body: `${cleanTitle}\n\n${cleanBody}`,
    }, 'Local item updated.').then((ok) => {
      if (ok) setEditing(false);
    });
  };

  return (
    <div className="comment-item description-comment">
      <div className="comment-header">
        <strong>{titleLabel}</strong>
        <time>{formatDate(createdAt)}</time>
        {editable && !editing && <button className="text-action" onClick={() => setEditing(true)}>Modify</button>}
      </div>
      {editing ? (
        <form className="inline-edit" onSubmit={save}>
          <input value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} aria-label="Local inbox title" />
          <textarea value={draftBody} onChange={(event) => setDraftBody(event.target.value)} rows={4} aria-label="Local inbox description" />
          <div className="inline-edit-actions">
            <button type="submit" disabled={!draftTitle.trim() || !draftBody.trim()}>Save</button>
            <button type="button" onClick={cancel}>Cancel</button>
          </div>
        </form>
      ) : (
        <div className="comment-body"><MarkdownBlock content={body} /></div>
      )}
    </div>
  );
}

function EditableComment({
  comment,
  editable,
  index,
  itemId,
  runAction,
}: {
  comment: any;
  editable: boolean;
  index: number;
  itemId: string;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
}) {
  const author = comment.author?.login ?? comment.author ?? 'unknown';
  const body = String(comment.body ?? '');
  const [editing, setEditing] = useState(false);
  const [draftAuthor, setDraftAuthor] = useState(author === 'unknown' ? '' : author);
  const [draftBody, setDraftBody] = useState(body);

  useEffect(() => {
    setDraftAuthor(author === 'unknown' ? '' : author);
    setDraftBody(body);
  }, [author, body]);

  const cancel = () => {
    setDraftAuthor(author === 'unknown' ? '' : author);
    setDraftBody(body);
    setEditing(false);
  };
  const save = (event: React.FormEvent) => {
    event.preventDefault();
    const cleanBody = draftBody.trim();
    const cleanAuthor = draftAuthor.trim();
    if (!cleanBody) return;
    runAction({
      action: 'comment_edit',
      provider: 'local',
      id: itemId,
      index,
      author: cleanAuthor,
      body: cleanBody,
    }, 'Comment updated.').then((ok) => {
      if (ok) setEditing(false);
    });
  };

  return (
    <div className={`comment-item${comment._description ? ' description-comment' : ''}`}>
      <div className="comment-header">
        <strong>{author}</strong>
        {comment._description && <span className="comment-tag">description</span>}
        <time>{formatDate(comment.createdAt ?? comment.created_at ?? comment.at)}</time>
        {comment.edited_at && <span>edited {formatDate(comment.edited_at)}</span>}
        {comment.url && <a href={comment.url} target="_blank" rel="noreferrer">Open</a>}
        {editable && !editing && <button className="text-action" onClick={() => setEditing(true)}>Modify</button>}
      </div>
      {editing ? (
        <form className="inline-edit" onSubmit={save}>
          <input value={draftAuthor} placeholder="Who wrote this comment?" onChange={(event) => setDraftAuthor(event.target.value)} />
          <textarea value={draftBody} onChange={(event) => setDraftBody(event.target.value)} rows={3} aria-label="Comment body" />
          <div className="inline-edit-actions">
            <button type="submit" disabled={!draftBody.trim()}>Save</button>
            <button type="button" onClick={cancel}>Cancel</button>
          </div>
        </form>
      ) : (
        <div className="comment-body"><MarkdownBlock content={body} /></div>
      )}
    </div>
  );
}

function TrashIcon() {
  return (
    <svg className="trash-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <path d="M6.5 1.5h3a.5.5 0 0 1 .5.5v1h3a.5.5 0 0 1 0 1h-.55l-.7 9.05a1.5 1.5 0 0 1-1.5 1.4H5.25a1.5 1.5 0 0 1-1.5-1.4L3.05 4H2.5a.5.5 0 0 1 0-1h3V2a.5.5 0 0 1 .5-.5Zm.5 1.5h2V2.5H7V3Zm-2.95 1 .69 8.98a.5.5 0 0 0 .5.47h5.52a.5.5 0 0 0 .5-.47L11.95 4H4.05ZM6.5 5.5a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5Zm3 0a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-1 0V6a.5.5 0 0 1 .5-.5Z" />
    </svg>
  );
}

function SourceMark({ provider }: { provider: string }) {
  if (provider === 'github') {
    return (
      <span className="source-mark github" title="GitHub" aria-label="GitHub">
        <svg viewBox="0 0 16 16" aria-hidden="true">
          <path d="M8 0.2a8 8 0 0 0-2.5 15.6c0.4 0.1 0.5-0.2 0.5-0.4v-1.5c-2 0.4-2.5-0.5-2.6-1-0.1-0.2-0.5-1-0.9-1.2-0.3-0.2-0.8-0.6 0-0.6 0.7 0 1.2 0.7 1.4 1 0.8 1.3 2.1 0.9 2.6 0.7 0.1-0.6 0.3-0.9 0.6-1.1-1.8-0.2-3.7-0.9-3.7-4a3.1 3.1 0 0 1 0.8-2.2 2.9 2.9 0 0 1 0.1-2.1s0.7-0.2 2.2 0.8a7.4 7.4 0 0 1 4 0c1.5-1 2.2-0.8 2.2-0.8 0.4 1 0.1 1.8 0.1 2.1a3.1 3.1 0 0 1 0.8 2.2c0 3.1-1.9 3.8-3.7 4 0.3 0.3 0.6 0.8 0.6 1.6v2.1c0 0.2 0.1 0.5 0.6 0.4A8 8 0 0 0 8 0.2Z" />
        </svg>
      </span>
    );
  }
  if (provider === 'ground') {
    return <span className="source-mark circle ground" title="Ground" aria-label="Ground">G</span>;
  }
  if (provider === 'horizon') {
    return <span className="source-mark circle horizon" title="Horizon" aria-label="Horizon">H</span>;
  }
  return <span className="source-mark local" title="Local" aria-label="Local">L</span>;
}

function CommentBox({
  item,
  runAction,
}: {
  item: any;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
}) {
  const [author, setAuthor] = useState('');
  const [body, setBody] = useState('');
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = body.trim();
    const cleanAuthor = author.trim();
    if (!trimmed || (item.provider === 'local' && !cleanAuthor)) return;
    runAction({
      action: 'comment',
      provider: item.provider,
      id: item.id,
      author: cleanAuthor,
      body: trimmed,
    }, item.provider === 'github' ? 'GitHub comment posted.' : 'Comment added.').then((ok) => {
      if (!ok) return;
      setBody('');
      setAuthor('');
    });
  };
  return (
    <form className={`comment-box ${item.provider}`} onSubmit={submit}>
      {item.provider === 'local' && <input value={author} placeholder="Who is writing this comment?" onChange={(event) => setAuthor(event.target.value)} />}
      <textarea value={body} placeholder={item.provider === 'github' ? 'Comment as authenticated GitHub user' : 'Comment'} onChange={(event) => setBody(event.target.value)} rows={2} />
      <button type="submit" disabled={!body.trim() || (item.provider === 'local' && !author.trim())}>Comment</button>
    </form>
  );
}

// The append-only history of an item: status/label/kind/body transitions.
function historyOf(item: any): any[] {
  const h = item?.metadata?.history;
  return Array.isArray(h) ? h : [];
}

// One-line, human description of a history transition.
function describeHistory(e: any): string {
  const from = e.from ? `${e.from}` : '—';
  switch (e.field) {
    case 'created': return 'opened this item';
    case 'deleted': return 'deleted this item';
    case 'status': return `status ${from} → ${e.to || '—'}`;
    case 'label': return `labels ${from} → ${e.to || '—'}`;
    case 'kind': return `type ${from} → ${e.to || '—'}`;
    case 'body': return e.note || 'edited the description';
    case 'edited': return e.note || 'edited fields';
    default: return e.note || e.field || 'changed';
  }
}

function HistoryRow({ entry }: { entry: any }) {
  return (
    <div className="history-row" title={formatDate(entry.at)}>
      <span className="history-dot" aria-hidden="true" />
      <span className="history-text"><strong>{entry.actor || 'system'}</strong> {describeHistory(entry)}</span>
      <time>{formatDate(entry.at)}</time>
    </div>
  );
}

// Merge comments and history into one chronological activity feed. Comments
// render (optionally editable); history transitions render as compact event
// rows so a reader sees what happened and when, in between the comments.
function ActivityTimeline({
  comments,
  history,
  editable,
  itemId,
  runAction,
  title = 'Activity',
  description,
}: {
  comments: any[];
  history: any[];
  editable: boolean;
  itemId: string;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
  title?: string;
  // Roadmap/task items have no standalone description field in the UI — their
  // body is shown as the FIRST comment in this feed (read-only), exactly like
  // the inbox, so description + comments + history read as one thread.
  description?: { body: string; author?: string; at?: string };
}) {
  const commentTime = (c: any) => String(c.at ?? c.created_at ?? c.createdAt ?? '');
  const allComments = description && description.body
    ? [{ author: description.author, at: description.at, body: description.body, _description: true }, ...comments]
    : comments;
  const entries = [
    ...allComments.map((c, i) => ({ kind: 'comment' as const, at: commentTime(c), c, i })),
    ...history.map((h, i) => ({ kind: 'event' as const, at: String(h.at ?? ''), h, i })),
  ].sort((a, b) => a.at.localeCompare(b.at));
  if (!entries.length) return null;
  return (
    <div className="issue-details">
      <div className="comment-thread">
        <div className="comment-thread-title">{title}</div>
        {entries.map((e) => e.kind === 'comment' ? (
          <EditableComment
            comment={e.c}
            editable={editable}
            index={e.i}
            itemId={itemId}
            key={`c-${e.c.id ?? e.i}`}
            runAction={runAction}
          />
        ) : (
          <HistoryRow entry={e.h} key={`h-${e.i}`} />
        ))}
      </div>
    </div>
  );
}

// Generic comment box for tasks and roadmap items: posts `{action:'comment'}`
// through the page's runAction. Author is required, matching the inbox.
function CommentComposer({
  id,
  runAction,
}: {
  id: string;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
}) {
  const [author, setAuthor] = useState('');
  const [body, setBody] = useState('');
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (!body.trim() || !author.trim()) return;
    runAction({ action: 'comment', id, author: author.trim(), body: body.trim() }, 'Comment added.').then((ok) => {
      if (!ok) return;
      setBody('');
      setAuthor('');
    });
  };
  return (
    <form className="comment-box" onSubmit={submit}>
      <input value={author} placeholder="Who is writing this comment?" onChange={(event) => setAuthor(event.target.value)} />
      <textarea value={body} placeholder="Add a comment (Markdown)…" onChange={(event) => setBody(event.target.value)} rows={2} />
      <button type="submit" disabled={!body.trim() || !author.trim()}>Comment</button>
    </form>
  );
}

// Compact, dim chip showing which run/session/subagent authored an item.
function ProvenanceChip({ provenance }: { provenance: any }) {
  if (!provenance || typeof provenance !== 'object') return null;
  const parts: string[] = [];
  if (provenance.run) parts.push(`run ${provenance.run}`);
  if (provenance.session) parts.push(String(provenance.session));
  if (provenance.subagent) parts.push(String(provenance.subagent));
  if (!parts.length) return null;
  return <span className="provenance-chip" title="Authoring run · session · subagent">{parts.join(' · ')}</span>;
}

function Transcripts({ state }: { state?: any }) {
  const runs = state?.runs ?? [];
  // Newest run first, so an in-progress run is at the top of the sidebar.
  const orderedRuns = [...runs].reverse();
  const [events, setEvents] = useState<any[] | null>(null);
  const [report, setReport] = useState<string>('');
  const [selected, setSelected] = useState<string>('');
  const selectedSession = useMemo(() => findSessionByRef(runs, selected), [runs, selected]);
  const selectedRun = useMemo(() => findRunBySessionRef(runs, selected), [runs, selected]);
  const [searchParams, setSearchParams] = useSearchParams();

  // Deep-link / selection: keep `selected` in sync with the URL.
  useEffect(() => {
    setSelected(searchParams.get('ref') ?? '');
  }, [searchParams]);

  // Live-tail the open transcript + its report: fetch on select, then poll so
  // new events and the final report appear without a manual page refresh.
  useEffect(() => {
    if (!selected) { setEvents(null); setReport(''); return; }
    let cancelled = false;
    const load = () => {
      getTranscript(selected).then((e) => { if (!cancelled) setEvents(e); }).catch(() => { if (!cancelled) setEvents([]); });
      getReport(selected).then((r) => { if (!cancelled) setReport(r?.markdown ?? ''); }).catch(() => { if (!cancelled) setReport(''); });
    };
    load();
    const id = setInterval(load, 3000);
    return () => { cancelled = true; clearInterval(id); };
  }, [selected]);

  return (
    <div className="page transcripts-page">
      <aside className="sidebar">
        <h2>Runs</h2>
        {orderedRuns.length === 0 && <p className="empty">No logs yet.</p>}
        <div className="session-list">
          {orderedRuns.map((run: any) => (
            <RunGroup key={run.id} run={run} selected={selected} onSelect={(ref) => {
              setSelected(ref);
              setSearchParams({ ref });
            }} />
          ))}
        </div>
      </aside>
      <TranscriptViewer
        events={events}
        harnesses={state?.harnesses ?? {}}
        report={report}
        run={selectedRun}
        selected={selected}
        session={selectedSession}
      />
    </div>
  );
}

// Native subagent events are tagged inline by the parsers (Claude: subagent_type
// + parent_tool_use_id; Codex: subagent_thread_id, folded in from the child's
// separate rollout file). We group a contiguous run of same-id events into their
// own collapsible sublog so a delegated agent reads as an independent unit.
function subagentId(event: any): string | null {
  const d = event?.data ?? {};
  return d.subagent_thread_id || d.parent_tool_use_id || null;
}
function subagentLabel(event: any): string {
  const d = event?.data ?? {};
  if (d.subagent_type) return String(d.subagent_type);
  if (d.subagent_thread_id) return `agent ${String(d.subagent_thread_id).slice(0, 8)}`;
  return 'subagent';
}

type LogGroup =
  | { sub: false; item: { event: any; idx: number } }
  | { sub: true; id: string; label: string; items: { event: any; idx: number }[] };

function groupSubagents(ordered: { event: any; idx: number }[] | null | undefined): LogGroup[] {
  // Collect ALL events sharing a subagent id into one sublog placed at the id's
  // first appearance — not just contiguous runs. Claude interleaves the parent's
  // own events (the spawning Task call, its narration) with the subagent's inline
  // events, so a contiguous grouping would split one agent into several blocks.
  const groups: LogGroup[] = [];
  const byId = new Map<string, { sub: true; id: string; label: string; items: { event: any; idx: number }[] }>();
  for (const entry of ordered ?? []) {
    const sid = subagentId(entry.event);
    if (!sid) {
      groups.push({ sub: false, item: entry });
      continue;
    }
    let group = byId.get(sid);
    if (!group) {
      group = { sub: true, id: sid, label: subagentLabel(entry.event), items: [] };
      byId.set(sid, group);
      groups.push(group);
    }
    // Upgrade the label if a later event names the descriptor (for codex only the
    // ingested rollout events carry subagent_type, not the first spawn event), so
    // the sublog shows "janitor" rather than "agent 1a2b3c4d".
    if (entry.event?.data?.subagent_type) group.label = String(entry.event.data.subagent_type);
    group.items.push(entry);
  }
  return groups;
}

function TranscriptViewer({
  events,
  harnesses,
  report,
  run,
  selected,
  session,
}: {
  events: any[] | null;
  harnesses?: Record<string, any>;
  report?: string;
  run?: any;
  selected: string;
  session?: any;
}) {
  const start = events?.find((event: any) => event.kind === 'session_start');
  const prompt = start?.data?.prompt ? stripAnsi(String(start.data.prompt)).trim() : '';
  const harness = start?.data?.harness ?? session?.meta?.data?.harness ?? '';
  const harnessConfig = harness ? harnesses?.[harness] : undefined;
  // Latest event on top: the live tail reads newest-first without scrolling.
  // The input prompt is rendered as a first-class panel, not as a log row.
  // Carry each event's original (append-only) index so rows get a *stable* key:
  // keying by the reversed position would reassign every row's open/collapsed
  // state to a different event whenever a new event is prepended, which both
  // reopens rows the user had closed and breaks native scroll anchoring.
  const ordered = events
    ? events
        .map((event: any, idx: number) => ({ event, idx }))
        .filter(({ event }: any) => event.kind !== 'session_start' && event.kind !== 'session_meta')
        .reverse()
    : events;
  const role = session ? sessionRole(session) : '';
  const groups = role === 'subagent' ? null : groupSubagents(ordered);
  const title = session?.meta?.name ?? session?.session ?? 'Log';
  // The engine stamps the real model onto session_meta/usage events; fall back to
  // it so the model shows even when the config never pinned one (and live, before
  // the session meta is written at the end of the run).
  const observedModel = events?.find((event: any) => event?.data?.model)?.data?.model;
  const model = session?.meta?.model ?? session?.meta?.effective_model ?? observedModel ?? harnessConfig?.model;
  // The session records the kind the engine ACTUALLY ran with; prefer it over the
  // current config (which may differ, or default to claude-code) so a Codex run
  // doesn't mislabel itself.
  const kind = session?.meta?.harness_kind ?? harnessConfig?.kind;
  const round = session?.meta?.round;
  const sessionId = session?.meta?.session_id ?? session?.meta?.data?.session_id;
  return (
    <section className={`transcript-viewer role-${role || 'none'}`}>
      <div className="panel-heading transcript-heading">
        <div className="transcript-headline">
          <div className="transcript-title-row">
            <RoleBadge role={role} />
            <h2>{title}</h2>
          </div>
          {session && (
            <div className="transcript-meta-row">
              {model && <span className="transcript-meta-chip">model <strong>{model}</strong></span>}
              {harness && (
                <span className="transcript-meta-chip">harness {harness}{kind ? ` · ${kind}` : ''}</span>
              )}
              {typeof round === 'number' && <span className="transcript-meta-chip">round {round + 1}</span>}
              {sessionId && <span className="transcript-meta-chip" title={String(sessionId)}>id {String(sessionId).slice(0, 8)}</span>}
              {session.usage && <span className="transcript-meta-chip">{formatUsage(session.usage)}</span>}
            </div>
          )}
          {selected && <p className="transcript-ref">{selected}</p>}
        </div>
      </div>
      {report && report.trim() && (
        <details className="log-panel report-panel" open>
          <summary>Report</summary>
          <div className="log-md"><MarkdownBlock content={report} /></div>
        </details>
      )}
      {events === null && <p className="empty transcript-empty">Select a session to inspect its events.</p>}
      {events && events.length === 0 && <p className="empty transcript-empty">No events in this session.</p>}
      {run?.stop?.reason && (
        <div className="notice warning log-stop-note">
          Run stopped at round {run.stop.round ?? '?'}: {run.stop.reason}
        </div>
      )}
      <div className="log-lines">
        {groups ? groups.map((group, i) =>
          group.sub ? (
            <details key={`sub-${group.id}-${i}`} className="log-panel subagent-sublog" open>
              <summary>
                <span className="role-badge role-subagent">S</span>
                <span className="subagent-name">{group.label}</span>
                {(() => {
                  const m = group.items.find(({ event }) => event?.data?.model)?.event?.data?.model;
                  return m ? <span className="subagent-model" title="Model used by this subagent">{m}</span> : null;
                })()}
                <span className="subagent-count">{group.items.length} events</span>
              </summary>
              <div className="log-lines sublog-lines">
                {group.items.map(({ event, idx }) => (
                  <TranscriptEvent key={idx} event={event} forceOpen={null} />
                ))}
              </div>
            </details>
          ) : (
            <TranscriptEvent key={group.item.idx} event={group.item.event} forceOpen={null} />
          ),
        ) : ordered?.map(({ event, idx }: any) => (
          <TranscriptEvent key={idx} event={event} forceOpen={null} />
        ))}
      </div>
      {session && <SessionParameters session={session} harness={harness} harnessConfig={harnessConfig} />}
      {prompt && (
        <details className="log-panel prompt-panel" open>
          <summary>Input Prompt</summary>
          <div className="log-md"><MarkdownBlock content={prompt} /></div>
        </details>
      )}
    </section>
  );
}

function findSessionByRef(runs: any[], ref: string): any | undefined {
  if (!ref) return undefined;
  const visit = (sessions: any[]): any | undefined => {
    for (const session of sessions ?? []) {
      if (session.ref === ref) return session;
      const child = visit(session.children ?? []);
      if (child) return child;
    }
    return undefined;
  };
  for (const run of runs ?? []) {
    const found = visit(run.sessions ?? []);
    if (found) return found;
  }
  return undefined;
}

function findRunBySessionRef(runs: any[], ref: string): any | undefined {
  if (!ref) return undefined;
  return runs.find((run) => !!findSessionByRef([run], ref));
}

function shortSha(value: any): string {
  const raw = typeof value === 'string' ? value.trim() : '';
  return raw ? raw.slice(0, 12) : '';
}

function formatProjectRevisions(value: any): string {
  if (!value || typeof value !== 'object') return '';
  return Object.entries(value)
    .filter(([, sha]) => typeof sha === 'string' && sha.trim())
    .map(([name, sha]) => `${name}:${String(sha).slice(0, 12)}`)
    .join(', ');
}

function formatArgList(value: any): string {
  return Array.isArray(value) ? value.map(String).join(' ') : '';
}

function summarizeHarnessOptions(options: any): string {
  if (!options || typeof options !== 'object') return '';
  return Object.entries(options)
    .filter(([key]) => !['env', 'pricing'].includes(key))
    .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : String(value)}`)
    .join(', ');
}

function SessionParameters({
  session,
  harness,
  harnessConfig,
}: {
  session: any;
  harness?: string;
  harnessConfig?: any;
}) {
  const meta = session.meta ?? {};
  const engineSession = meta.engine_session_id ?? meta.session_id ?? meta.data?.session_id;
  // An interrupted session never wrote an end; measure it to its last activity.
  const durEnd = session.status === 'interrupted' ? session.last_at : session.ended_at;
  const rows = [
    ['Run', session.run],
    ['Session', session.session],
    ['Role', meta.role],
    ['Name', meta.name],
    ['Round', typeof meta.round === 'number' ? String(meta.round + 1) : ''],
    ['Project', meta.project],
    ['Task', meta.task_id],
    ['Parent', session.parent],
    ['Status', session.status],
    ['Started', formatDateTime(session.started_at)],
    ['Ended', formatDateTime(session.ended_at)],
    ['Duration', formatDuration(session.started_at, durEnd)],
    ['Workspace SHA', shortSha(meta.workspace_sha)],
    ['Project SHAs', formatProjectRevisions(meta.project_revisions)],
    ['Harness', harness],
    ['Harness kind', session.meta?.harness_kind ?? harnessConfig?.kind],
    // Prefer the model the engine actually used over the configured one.
    ['Model', session.model ?? meta.model ?? meta.effective_model ?? harnessConfig?.model],
    ['Command', harnessConfig?.command],
    ['Args', formatArgList(harnessConfig?.args)],
    ['Options', summarizeHarnessOptions(harnessConfig?.options)],
    ['Engine session', engineSession],
    ['Usage', formatUsageBrief(session.usage)],
  ].filter(([, value]) => value !== undefined && value !== null && String(value).trim() !== '');
  if (rows.length === 0) return null;
  return (
    <section className="session-params">
      <table>
        <tbody>
          {rows.map(([label, value]) => (
            <tr key={label}>
              <th>{label}</th>
              <td>{String(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// Mirror the UI status palette (the softer desaturated chip colors) so the log
// pills read consistently with the rest of the dashboard. The pill bg/ring are
// derived from this text color via color-mix in .log-pill.
const EVENT_COLORS: Record<string, string> = {
  thinking: '#6d28d9', text: '#1d4ed8', tool_call: '#c2410c',
  tool_result: '#047857', error: '#b91c1c', session_start: '#475569', session_end: '#475569',
};
// Render text/thinking as markdown (after stripping terminal ANSI); everything
// else stays monospace. The input prompt is rendered by TranscriptViewer.
const MARKDOWN_KINDS = new Set(['text', 'thinking']);

// MCP tool names arrive as `mcp__<server>__<tool>`, which is far too long for a
// pill; show just the tool (full name stays available as a tooltip).
function shortTool(name: string): string {
  if (!name) return name;
  if (name.startsWith('mcp__')) {
    const rest = name.slice(5);
    const i = rest.indexOf('__');
    return i >= 0 ? rest.slice(i + 2) : rest;
  }
  return name;
}

// A compact, scannable model label for a sidebar chip, e.g.
// "claude-opus-4-8[1m]" -> "opus 4.8", "gpt-5-codex" -> "gpt 5 codex".
function shortModel(model?: string): string {
  if (!model) return '';
  let m = String(model).trim();
  m = m.replace(/\[[^\]]*\]/g, '');   // drop [1m]-style context tags
  m = m.replace(/-(\d{8})$/, '');     // drop a trailing date stamp
  const fam = m.match(/(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?/i);
  if (fam) {
    const ver = [fam[2], fam[3]].filter(Boolean).join('.');
    return `${fam[1].toLowerCase()} ${ver}`.trim();
  }
  return m.replace(/^claude-/i, '').replace(/-/g, ' ').trim();
}

// A short semantic tag for an event so the log is scannable: spot where a
// subagent was dispatched, the inbox was touched, Lean was built, etc.
const TAG_COLORS: Record<string, string> = {
  subagent: '#7c3aed', inbox: '#0891b2', dag: '#c2410c', blueprint: '#1d4ed8',
  search: '#0d9488', lean: '#15803d', git: '#9333ea', skill: '#b45309', lsp: '#047857', mcp: '#047857',
};
function commandTag(event: any): string | null {
  if (event.kind !== 'tool_call') return null;
  const tool = String(event.tool || '');
  if (tool === 'Skill') return 'skill';
  if (tool.startsWith('mcp__lean-lsp')) return 'lsp';
  if (tool.startsWith('mcp__')) return 'mcp';
  if (tool === 'Bash') {
    const cmd = String(event.data?.input?.command ?? event.data?.command ?? '');
    if (/horizon-subagent\.py/.test(cmd)) return 'subagent';
    if (/\bhorizon\s+inbox\b/.test(cmd)) return 'inbox';
    if (/\bhorizon\s+leandag\b/.test(cmd)) return 'dag';
    if (/\bhorizon\s+blueprint\b/.test(cmd)) return 'blueprint';
    if (/\bhorizon\s+search\b/.test(cmd)) return 'search';
    if (/\blake\b|\blean\b/.test(cmd)) return 'lean';
    if (/\bgit\b/.test(cmd)) return 'git';
  }
  return null;
}

// A tool_result's content may be a string, an array of {type,text} blocks, or an
// object; flatten any of those to displayable text.
function contentToText(c: any): string {
  if (c == null) return '';
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) return c.map((b) => (typeof b === 'string' ? b : b?.text ?? JSON.stringify(b))).join('\n');
  return JSON.stringify(c, null, 2);
}

// The displayable body of an event. Plain text events carry `text`; tool_call /
// tool_result carry their payload under `data` (input/content/command/changes
// across the claude/codex parsers), which must be surfaced too — otherwise
// every tool step renders as an empty "—".
function eventBody(event: any): string {
  // The input prompt rides on session_start; show it (and nothing else) there.
  if (event.kind === 'session_start') return event.data?.prompt ? String(event.data.prompt) : '';
  if (event.text && String(event.text).trim()) return String(event.text);
  const d = event.data || {};
  if (d.content != null) return contentToText(d.content);
  if (d.input != null) return typeof d.input === 'string' ? d.input : JSON.stringify(d.input, null, 2);
  if (d.command != null) return typeof d.command === 'string' ? d.command : JSON.stringify(d.command, null, 2);
  if (d.changes != null) return JSON.stringify(d.changes, null, 2);
  if (typeof d === 'object' && Object.keys(d).length) return JSON.stringify(d, null, 2);
  return '';
}

function toolInput(event: any): any {
  const data = event.data || {};
  if (data.input && typeof data.input === 'object') return data.input;
  if (data.command != null) return { command: data.command };
  if (data.changes != null) return { changes: data.changes };
  return data;
}

function ShellCommandBlock({ command }: { command: string }) {
  const lines = command.split('\n');
  return (
    <pre className="log-code log-shell">
      {lines.map((line, index) => (
        <span className="shell-line" key={index}>
          <span className="shell-prompt">$</span>
          <span>{line || ' '}</span>
        </span>
      ))}
    </pre>
  );
}

function FileToolCard({ label, path, details }: { label: string; path: string; details?: string }) {
  return (
    <div className="log-file-card">
      <span className="log-file-action">{label}</span>
      <code>{path}</code>
      {details && <span>{details}</span>}
    </div>
  );
}

function WrittenFileView({ path, content }: { path: string; content: string }) {
  const isMarkdown = /\.md(?:$|\?)/.test(path);
  return (
    <div className="log-tool-view">
      <FileToolCard label="write" path={path} details={`${content.length.toLocaleString()} chars`} />
      {isMarkdown ? (
        <div className="log-written-md"><MarkdownBlock content={content} /></div>
      ) : (
        <pre className="log-output">{content}</pre>
      )}
    </div>
  );
}

function diffLines(oldText: string, newText: string): { kind: 'same' | 'old' | 'new'; text: string }[] {
  const oldLines = oldText.split('\n');
  const newLines = newText.split('\n');
  let start = 0;
  while (start < oldLines.length && start < newLines.length && oldLines[start] === newLines[start]) start += 1;
  let oldEnd = oldLines.length - 1;
  let newEnd = newLines.length - 1;
  while (oldEnd >= start && newEnd >= start && oldLines[oldEnd] === newLines[newEnd]) {
    oldEnd -= 1;
    newEnd -= 1;
  }
  const out: { kind: 'same' | 'old' | 'new'; text: string }[] = [];
  const before = Math.max(0, start - 2);
  for (let i = before; i < start; i += 1) out.push({ kind: 'same', text: oldLines[i] });
  for (let i = start; i <= oldEnd; i += 1) out.push({ kind: 'old', text: oldLines[i] });
  for (let i = start; i <= newEnd; i += 1) out.push({ kind: 'new', text: newLines[i] });
  const afterEnd = Math.min(oldLines.length - 1, oldEnd + 2);
  for (let i = oldEnd + 1; i <= afterEnd; i += 1) out.push({ kind: 'same', text: oldLines[i] });
  if (out.length === 0 && oldText === newText) out.push({ kind: 'same', text: '(no textual change)' });
  return out;
}

function UnifiedDiff({ filePath, oldText, newText }: { filePath?: string; oldText: string; newText: string }) {
  return (
    <div className="log-diff">
      {filePath && <div className="log-diff-file">{filePath}</div>}
      <pre>
        {diffLines(oldText, newText).map((line, index) => (
          <span className={`diff-line ${line.kind}`} key={index}>
            <span className="diff-marker">{line.kind === 'old' ? '-' : line.kind === 'new' ? '+' : ' '}</span>
            <span>{line.text || ' '}</span>
          </span>
        ))}
      </pre>
    </div>
  );
}

function JsonBlock({ value }: { value: any }) {
  const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  return <pre className="log-pre">{text}</pre>;
}

function usageNumbers(usage: any) {
  return {
    tokensIn: Number(usage?.tokens_in ?? 0),
    tokensOut: Number(usage?.tokens_out ?? 0),
    cachedIn: Number(usage?.cached_tokens_in ?? 0),
    reasoningOut: Number(usage?.reasoning_tokens_out ?? 0),
    cost: typeof usage?.cost_usd === 'number' ? usage.cost_usd : null,
  };
}

function UsageChips({ usage }: { usage: any }) {
  const { tokensIn, tokensOut, cachedIn, reasoningOut, cost } = usageNumbers(usage);
  if (!tokensIn && !tokensOut && cost === null) return null;
  const netIn = Math.max(tokensIn - cachedIn, 0);
  return (
    <>
      {(tokensIn || tokensOut) && <span className="meta-chip" title={`${tokensIn.toLocaleString()} total input incl. cached`}>{netIn.toLocaleString()} in</span>}
      {(tokensIn || tokensOut) && <span className="meta-chip">{tokensOut.toLocaleString()} out</span>}
      {cachedIn > 0 && <span className="meta-chip muted">{cachedIn.toLocaleString()} cached</span>}
      {reasoningOut > 0 && <span className="meta-chip muted">{reasoningOut.toLocaleString()} reasoning</span>}
      {cost !== null && <span className="meta-chip cost">${cost.toFixed(4)}</span>}
    </>
  );
}

function UsageBlock({ usage }: { usage: any }) {
  return (
    <div className="usage-block">
      <UsageChips usage={usage} />
    </div>
  );
}

function ToolCallView({ event, text }: { event: any; text: string }) {
  const name = String(event.tool || '');
  const input = toolInput(event);
  if (name === 'Bash' || name === 'command_execution') {
    const command = String(input.command ?? '');
    return (
      <div className="log-tool-view">
        {input.description && <div className="log-tool-summary">{String(input.description)}</div>}
        {command ? <ShellCommandBlock command={command} /> : <JsonBlock value={input} />}
      </div>
    );
  }
  if (name === 'Read' || name === 'VIEW_FILE') {
    const details = [
      input.offset != null ? `offset ${input.offset}` : '',
      input.limit != null ? `limit ${input.limit}` : '',
    ].filter(Boolean).join(' · ');
    return <FileToolCard label="read" path={String(input.file_path ?? input.path ?? '')} details={details} />;
  }
  if (name === 'Edit' || name === 'MultiEdit' || name === 'Write' || name === 'file_change') {
    if (typeof input.old_string === 'string' && typeof input.new_string === 'string') {
      return <UnifiedDiff filePath={String(input.file_path ?? '')} oldText={input.old_string} newText={input.new_string} />;
    }
    if (name === 'Write' && typeof input.content === 'string') {
      return <WrittenFileView path={String(input.file_path ?? '')} content={input.content} />;
    }
    return <JsonBlock value={input.changes ?? input} />;
  }
  if (name === 'Grep' || name === 'GREP_SEARCH' || name === 'Glob') {
    return <FileToolCard label={name.toLowerCase()} path={String(input.pattern ?? input.path ?? input.glob ?? text)} />;
  }
  return <JsonBlock value={input || text} />;
}

function ToolResultView({ event, text }: { event: any; text: string }) {
  const exitCode = event.data?.exit_code;
  return (
    <div className="log-tool-result">
      {exitCode !== undefined && <span className={`exit-code exit-${exitCode === 0 ? 'ok' : 'bad'}`}>exit {String(exitCode)}</span>}
      <pre className="log-output">{text || 'no output'}</pre>
    </div>
  );
}

function EventBodyView({ event, text }: { event: any; text: string }) {
  if (event.kind === 'usage') return <UsageBlock usage={event.usage ?? event.data} />;
  if (event.kind === 'tool_call' && event.data?.actor && text) {
    return (
      <div className="log-system-event">
        <div>{text}</div>
        <details>
          <summary>Details</summary>
          <JsonBlock value={event.data} />
        </details>
      </div>
    );
  }
  if (event.kind === 'tool_call') return <ToolCallView event={event} text={text} />;
  if (event.kind === 'tool_result') return <ToolResultView event={event} text={text} />;
  if (MARKDOWN_KINDS.has(event.kind)) return <div className="log-md"><MarkdownBlock content={text} /></div>;
  return <pre className="log-pre">{text}</pre>;
}

function TranscriptEvent({ event, forceOpen }: { event: any; forceOpen: boolean | null }) {
  const text = stripAnsi(eventBody(event)).trim();
  const hasBody = text.length > 0;
  const long = text.length > 280 || text.includes('\n');
  const [open, setOpen] = useState(!long);
  useEffect(() => { if (forceOpen !== null) setOpen(forceOpen); }, [forceOpen]);
  const color = EVENT_COLORS[event.kind] ?? 'var(--text-muted)';
  const fullLabel = event.tool ? event.tool : event.kind;
  const label = event.tool ? shortTool(event.tool) : event.kind === 'session_start' ? 'input' : event.kind;
  const tag = commandTag(event);
  const preview = text.slice(0, 120).replace(/\s+/g, ' ');
  // Click anywhere in the row to expand/collapse a long event — but don't fight
  // text selection or clicks on links/buttons inside the body.
  const toggle = (e: React.MouseEvent) => {
    if (!long) return;
    if (window.getSelection()?.toString()) return;
    if ((e.target as HTMLElement).closest('a, button')) return;
    setOpen((v) => !v);
  };
  return (
    <div className="log-line" onClick={toggle} style={{ cursor: long ? 'pointer' : 'default' }}>
      <span className="log-ts">{formatTime(event.at)}</span>
      <span className="log-pill" style={{ color }} title={fullLabel}><span>{label}</span></span>
      {tag && <span className="log-tag" style={{ color: TAG_COLORS[tag] ?? 'var(--text-muted)' }}>{tag}</span>}
      <div className="log-content">
        {hasBody ? (
          <>
            {long && !open && <span className="ev-preview">{preview}…</span>}
            {open && (
              <EventBodyView event={event} text={text} />
            )}
          </>
        ) : (
          <span className="log-empty-body">
            {[
              event.kind === 'thinking' ? 'thinking not exposed by this engine' : '',
              formatUsage(event.usage),
            ].filter(Boolean).join(' · ') || '—'}
          </span>
        )}
        {hasBody && formatUsage(event.usage) && <span className="log-usage">{formatUsage(event.usage)}</span>}
      </div>
    </div>
  );
}

// A run row that collapses to just its id (e.g. "0001") by default; clicking
// expands the whole session tree (all steps).
function RunGroup({ run, selected, onSelect }: { run: any; selected: string; onSelect: (ref: string) => void }) {
  const [open, setOpen] = useState(false);
  const sessions = run.sessions ?? [];
  return (
    <div className="transcript-run-group">
      <button className="run-header button-reset" onClick={() => setOpen((v) => !v)}>
        <span className="ev-caret">{open ? '▾' : '▸'}</span>
        <strong>{run.id}</strong>
        <StatusIcon value={run.status} />
        <span className="meta-chip">{run.session_count ?? 0} sessions</span>
        <UsageChips usage={run.usage} />
      </button>
      {open && (
        <div className="run-sessions-tree">
          <SelectableSessionTree sessions={sessions} selected={selected} onSelect={onSelect} />
        </div>
      )}
    </div>
  );
}

function SelectableSessionTree({ sessions, selected, onSelect }: { sessions: any[], selected: string, onSelect: (ref: string) => void }) {
  if (sessions.length === 0) return null;
  return (
    <div className="selectable-tree">
      {sessions.map((session) => (
        <SessionNode key={`${session.parent}-${session.session}`} session={session} selected={selected} onSelect={onSelect} />
      ))}
    </div>
  );
}

// The session's collaboration role drives its colour bar + logo in the sidebar,
// mirroring the inbox's source bars so ground / horizon / publish / subagent
// rounds are distinguishable at a glance. `subagent` is inferred from nesting
// (a child session) when meta hasn't been written yet (still running).
function sessionRole(session: any): string {
  const raw = String(session.meta?.role ?? '').toLowerCase();
  if (raw) return raw;
  const name = String(session.session ?? '').toLowerCase();
  if (name.includes('ground')) return 'ground';
  if (name.includes('horizon')) return 'horizon';
  if (name.includes('system')) return 'system';
  if (name.includes('publish')) return 'publish';
  return session.parent ? 'subagent' : '';
}

function displaySessionName(session: any): string {
  const raw = String(session.meta?.name ?? session.session ?? '');
  const match = raw.match(/^(\d+)-(.+)$/);
  if (!match) return raw;
  const index = match[1].padStart(5, '0');
  const role = sessionRole(session);
  const label = role && role !== 'subagent' ? role : match[2].replace(/-/g, ' ');
  return `${index} ${label}`;
}

const ROLE_LOGO: Record<string, string> = {
  ground: 'G', horizon: 'H', publish: 'P', subagent: 'S', system: '⚙',
};

function RoleBadge({ role }: { role: string }) {
  if (!role) return null;
  if (role === 'system') {
    return (
      <span className="role-badge role-system" title="system">
        <svg width="11.5" height="11.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
          <circle cx="12" cy="12" r="3"></circle>
          <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"></path>
        </svg>
      </span>
    );
  }
  return <span className={`role-badge role-${role}`} title={role}>{ROLE_LOGO[role] ?? role.charAt(0).toUpperCase()}</span>;
}

function SessionNode({ session, selected, onSelect }: { session: any; selected: string; onSelect: (ref: string) => void }) {
  const hasChildren = (session.children?.length ?? 0) > 0;
  const round = typeof session.meta?.round === 'number' ? `r${session.meta.round}` : '';
  const role = sessionRole(session);
  const displayName = displaySessionName(session);
  const model = shortModel(session.model);
  // An interrupted session never wrote an end, so measure it to its last
  // activity instead of leaving the duration stuck on "running".
  const durEnd = session.status === 'interrupted' ? session.last_at : session.ended_at;
  return (
    <div className="session-node">
      <div className={`session-line role-${role || 'none'} ${selected === session.ref ? 'active' : ''}`}>
        <button className="session-main button-reset" onClick={() => session.ref && onSelect(session.ref)} style={{ cursor: session.ref ? 'pointer' : 'default' }}>
          <div className="session-row1">
            <RoleBadge role={role} />
            <StatusIcon value={session.status} />
            <strong title={session.meta?.name ?? session.session}>{displayName}</strong>
          </div>
          <div className="session-row2">
            {model && <span className="meta-chip model-chip" title={session.model}>{model}</span>}
            {round && <span className="meta-chip">{round}</span>}
            {session.started_at && <span className="meta-chip" title={`${formatTime(session.started_at)}-${formatTime(session.ended_at)}`}>{formatTime(session.started_at)}</span>}
            {session.started_at && <span className="meta-chip">{formatDuration(session.started_at, durEnd)}</span>}
            <UsageChips usage={session.usage} />
          </div>
        </button>
      </div>
      {hasChildren && (
        <div className="session-children">
          <SelectableSessionTree sessions={session.children} selected={selected} onSelect={onSelect} />
        </div>
      )}
    </div>
  );
}

function RunList({ runs }: { runs: any[] }) {
  if (runs.length === 0) return <p className="empty">No runs yet.</p>;
  return (
    <div className="run-list">
      {runs.map((run) => {
        const firstRef = firstSessionRef(run.sessions ?? []);
        const rounds = `${run.rounds_completed ?? 0}/${run.rounds_requested ?? '?'}`;
        return (
          <details key={run.id} className="run-card">
            <summary>
              <div className="run-main">
                <Badge>{run.id}</Badge>
                <Status value={run.status} />
                <strong>{run.focus?.task || (run.focus?.tasks ?? []).join(', ') || (run.focus?.projects ?? []).join(', ') || 'Workspace run'}</strong>
              </div>
              <div className="run-meta">
                <span>rounds {rounds}</span>
                <span>{run.session_count ?? 0} sessions</span>
                <span>{formatUsageBrief(run.usage)}</span>
                <time>{formatDate(run.created_at)}</time>
                {firstRef && <Link to={`/logs?ref=${encodeURIComponent(firstRef)}`}>Open logs</Link>}
              </div>
            </summary>
            <SessionTree sessions={run.sessions ?? []} />
          </details>
        );
      })}
    </div>
  );
}

function SessionTree({ sessions }: { sessions: any[] }) {
  if (sessions.length === 0) return <p className="empty run-sessions">No sessions recorded.</p>;
  return (
    <div className="run-sessions">
      {sessions.map((session) => (
        <div key={`${session.parent}-${session.session}`} className="session-node">
          <div className="session-line">
            <Status value={session.status} />
            <span>{session.meta?.role ?? session.meta?.name ?? 'session'}</span>
            <strong title={session.meta?.name ?? session.session}>{displaySessionName(session)}</strong>
            {typeof session.meta?.round === 'number' && <span>round {session.meta.round + 1}</span>}
            <span>{formatUsageBrief(session.usage)}</span>
            {session.ref && <Link to={`/logs?ref=${encodeURIComponent(session.ref)}`}>Open</Link>}
          </div>
          {session.children?.length > 0 && <SessionTree sessions={session.children} />}
        </div>
      ))}
    </div>
  );
}

function TaskList({ tasks, compact = false }: { tasks: any[]; compact?: boolean }) {
  if (tasks.length === 0) return <p className="empty">No tasks.</p>;
  if (compact) {
    return (
      <div className="mini-list">
        {tasks.map((task) => (
          <div key={task.id} className="mini-row">
            <Status value={task.status} />
            <Badge>{task.id}</Badge>
            <span className="mini-title clamp-1"><InlineMarkdown content={task.title || task.objective || task.id} /></span>
            <span className={`priority-chip priority-${task.priority || 'normal'}`}>{task.priority || 'normal'}</span>
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="task-list">
      {tasks.map((task) => (
        <div key={task.id} className="task-card">
          <div className="task-title">
            <Badge>{task.id}</Badge>
            <Status value={task.status} />
            <strong className="clamp-1">{task.title || task.objective || task.id}</strong>
          </div>
          <div className="task-meta">
            <span>{(task.projects ?? [task.project]).filter(Boolean).join(', ')}</span>
            {task.priority && <Badge>{task.priority}</Badge>}
            {(task.roadmap_refs ?? []).map((ref: string) => <Badge key={ref}>{ref}</Badge>)}
          </div>
          {task.explanation && <p>{task.explanation}</p>}
          {task.write_set && (
            <div className="task-files">
              {(task.write_set.files ?? []).slice(0, 4).map((file: string) => <code key={file}>{file}</code>)}
              {(task.write_set.files ?? []).length > 4 && <span>+{task.write_set.files.length - 4} more</span>}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function RoadmapFocus({ items, compact = false }: { items: any[]; compact?: boolean }) {
  if (items.length === 0) return <p className="empty">No roadmap items.</p>;
  if (compact) {
    return (
      <div className="mini-list">
        {items.map((item) => (
          <div key={item.id} className="mini-row">
            <Status value={item.status} />
            <Badge>{item.id}</Badge>
            <span className="mini-title clamp-1"><InlineMarkdown content={item.title} /></span>
          </div>
        ))}
      </div>
    );
  }
  return (
    <div className="roadmap-mini-list">
      {items.map((item) => (
        <div key={item.id} className="roadmap-mini">
          <div className="roadmap-mini-main">
            <div className="roadmap-mini-title">
              <Badge>{item.id}</Badge>
              <strong className="clamp-1"><InlineMarkdown content={item.title} /></strong>
            </div>
            {(item.projects ?? []).length > 0 && (
              <div className="roadmap-mini-projects" aria-label="Projects">
                {(item.projects ?? []).map((project: string) => <Badge key={project}>{project}</Badge>)}
              </div>
            )}
          </div>
          <Status value={item.status} />
          {item.summary && <p>{item.summary}</p>}
        </div>
      ))}
    </div>
  );
}

function RoadmapItemCard({ item, runAction, projects = [] }: { item: any, runAction?: any, projects?: string[] }) {
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(item.title);
  const [draftSummary, setDraftSummary] = useState(item.summary || '');
  const [draftAuthor, setDraftAuthor] = useState(item.metadata?.author || '');
  const [draftProjects, setDraftProjects] = useState<string[]>(item.projects ?? []);
  const [visibleStatus, setVisibleStatus] = useState(item.status);
  const [priority, setPriority] = useState(item.priority || 'normal');

  const updateStatus = (nextStatus: string) => {
    const previous = visibleStatus;
    setVisibleStatus(nextStatus);
    runAction?.({ action: 'status', id: item.id, status: nextStatus }).then((ok: boolean) => {
      if (!ok) setVisibleStatus(previous);
    });
  };

  const updatePriority = (next: string) => {
    const previous = priority;
    setPriority(next);
    runAction?.({ action: 'edit', id: item.id, priority: next }).then((ok: boolean) => {
      if (!ok) setPriority(previous);
    });
  };

  // Re-sync inline-editable fields when fresh props arrive (e.g. after another
  // copy of the same shared item is updated and the global state reloads).
  useEffect(() => {
    setVisibleStatus(item.status);
    setPriority(item.priority || 'normal');
  }, [item.status, item.priority]);

  const deleteItem = () => {
    if (!window.confirm(`Delete roadmap item ${item.id}?`)) return;
    runAction?.({ action: 'delete', id: item.id });
  };

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    if (!draftTitle.trim() || !draftAuthor.trim()) return;
    runAction?.({
      action: 'edit',
      id: item.id,
      title: draftTitle.trim(),
      summary: draftSummary.trim(),
      author: draftAuthor.trim(),
      projects: draftProjects
    }).then((ok: boolean) => {
      if (ok) setEditing(false);
    });
  };

  const cancel = () => {
    setDraftTitle(item.title);
    setDraftSummary(item.summary || '');
    setDraftAuthor(item.metadata?.author || '');
    setDraftProjects(item.projects ?? []);
    setEditing(false);
  };

  return (
    <details className="roadmap-card" open={editing}>
      <summary style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div className="roadmap-title" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: 1, minWidth: 0 }}>
          {!STATIC && runAction ? (
            <select className={`chip-select state ${visibleStatus}`} style={{ color: statusColor(visibleStatus), borderColor: statusColor(visibleStatus) }} value={visibleStatus} onChange={(e) => updateStatus(e.target.value)}>
              <option className="state-active" value="active">active</option>
              <option className="state-pending" value="pending">pending</option>
              <option className="state-blocked" value="blocked">blocked</option>
              <option className="state-completed" value="done">done</option>
              <option className="state-rejected" value="rejected">rejected</option>
            </select>
          ) : <Status value={item.status} />}
          {!STATIC && runAction ? (
            <select className={`chip-select priority priority-${priority}`} value={priority} onChange={(e) => updatePriority(e.target.value)} aria-label="Priority">
              <option value="urgent">urgent</option>
              <option value="high">high</option>
              <option value="normal">normal</option>
              <option value="low">low</option>
            </select>
          ) : <span className={`priority-chip priority-${priority}`}>{priority}</span>}
          <strong className="clamp-1"><InlineMarkdown content={item.title} /></strong>
          {item.metadata?.author && <span className="author-tag">by {item.metadata.author}</span>}
          <ProvenanceChip provenance={item.metadata?.provenance} />
          {item.metadata?.updated_at && <span className="author-tag" style={{ marginLeft: 'auto' }}>updated {new Date(item.metadata.updated_at).toLocaleDateString()}</span>}
        </div>
        {!STATIC && runAction && (
           <button className="icon-danger button-reset" onClick={deleteItem} title="Delete roadmap item" aria-label="Delete roadmap item"><TrashIcon /></button>
        )}
      </summary>
      <div className="roadmap-body">
        {editing ? (
          <form className="inline-edit" onSubmit={save}>
            <label className="composer-label">Projects</label>
            <MultiProjectSelect value={draftProjects} onChange={setDraftProjects} projects={projects} />
            <input value={draftAuthor} onChange={(e) => setDraftAuthor(e.target.value)} placeholder="Author (required)" />
            <input value={draftTitle} onChange={(e) => setDraftTitle(e.target.value)} placeholder="Title" />
            <textarea value={draftSummary} onChange={(e) => setDraftSummary(e.target.value)} rows={4} placeholder="Summary (Markdown)" />
            <div className="inline-edit-actions">
              <button type="submit" disabled={!draftTitle.trim() || !draftAuthor.trim()}>Save</button>
              <button type="button" onClick={cancel}>Cancel</button>
            </div>
          </form>
        ) : (
          <div className="roadmap-summary">
             <div className="item-meta">
               <span>{item.id}</span>
               {item.metadata?.author && <span>by {item.metadata.author}</span>}
               <ProvenanceChip provenance={item.metadata?.provenance} />
               {item.metadata?.created_at && <span>created {formatDate(item.metadata.created_at)}</span>}
               {item.metadata?.updated_at && <span>updated {formatDate(item.metadata.updated_at)}</span>}
             </div>
             <ActivityTimeline
               description={item.summary ? { body: item.summary, author: item.metadata?.author, at: item.metadata?.created_at } : undefined}
               comments={item.metadata?.comments ?? []}
               history={historyOf(item)}
               editable={false}
               itemId={item.id}
               runAction={runAction}
             />
             {!item.summary && (item.metadata?.comments ?? []).length === 0 && historyOf(item).length === 0 && <p className="empty">No summary.</p>}
             {!STATIC && runAction && <CommentComposer id={item.id} runAction={runAction} />}
             {!STATIC && runAction && <button className="text-action" onClick={() => setEditing(true)}>Modify</button>}
          </div>
        )}
      </div>
    </details>
  );
}

function FieldChips({ label, values }: { label: string; values: string[] }) {
  return (
    <div className="field-chips">
      <span>{label}</span>
      <div>{values.length ? values.map((value) => <Badge key={value}>{value}</Badge>) : <span className="empty">-</span>}</div>
    </div>
  );
}

function InboxSummary({ local, github }: { local: any[]; github: any[] }) {
  const open = (xs: any[]) => xs.filter((i) => i.status === 'open');
  const recent = [...open(local), ...open(github)]
    .sort((a, b) => String(b.updated_at ?? b.created_at ?? '').localeCompare(String(a.updated_at ?? a.created_at ?? '')))
    .slice(0, 4);
  return (
    <div>
      <div className="inbox-summary-boxes">
        <div className="inbox-summary-box">
          <span className="inbox-summary-label">Local</span>
          <span><strong>{open(local).length}</strong> open</span>
          <em>/ {local.length}</em>
        </div>
        <div className="inbox-summary-box">
          <span className="inbox-summary-label">GitHub</span>
          <span><strong>{open(github).length}</strong> open</span>
          <em>/ {github.length}</em>
        </div>
      </div>
      {recent.length ? (
        <div className="inbox-mini-list">
          {recent.map((i) => {
            const author = inboxAuthor(i);
            const pseudoProvider = author?.toLowerCase() === 'ground' ? 'ground' : author?.toLowerCase() === 'horizon' ? 'horizon' : i.provider;
            const { title } = inboxTitleAndBody(i);
            const gate = inboxGate(i.labels ?? []);
            return (
              <div key={`${i.provider}-${i.id}`} className={`inbox-mini-row source-${pseudoProvider}`}>
                <SourceMark provider={pseudoProvider} />
                <span className={`type-chip type-${i.kind}`}>{i.kind}</span>
                {gate !== 'clear' && <Status value={gate} label={gateLabel(gate)} />}
                <span className="mini-title clamp-1"><InlineMarkdown content={title} /></span>
              </div>
            );
          })}
        </div>
      ) : <p className="empty">No open items.</p>}
    </div>
  );
}

function EventList({ events, compact = false }: { events: any[]; compact?: boolean }) {
  if (events.length === 0) return <p className="empty">No events.</p>;
  return (
    <div className={compact ? 'event-list compact' : 'event-list'}>
      {events.map((event, idx) => (
        <div key={`${event.id}-${idx}`} className="event-row">
          <Badge>{event.type}</Badge>
          <span>{event.id}</span>
          <time>{formatDate(event.created_at)}</time>
        </div>
      ))}
    </div>
  );
}

function ReportList({ reports }: { reports: string[] }) {
  if (reports.length === 0) return <p className="empty">No reports.</p>;
  return (
    <div className="report-list">
      {reports.map((report) => <a key={report} href={`reports/${report}.md`}>{report}</a>)}
    </div>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="badge">{children}</span>;
}

// Render a single line of markdown (bold/italic/code/math/links) without the
// block <p> wrapper — for titles in lists and cards.
function InlineMarkdown({ content }: { content: string }) {
  const html = markdownToHtml(content ?? '').replace(/^\s*<p>/, '').replace(/<\/p>\s*$/, '');
  return <span className="inline-md" dangerouslySetInnerHTML={{ __html: html }} />;
}

// Compact status as a glyph: ✓ done, ✕ failed, ◌ running (spins),
// ⊘ interrupted (stopped early, not active), ○ otherwise.
function StatusIcon({ value, title }: { value: string; title?: string }) {
  const map: Record<string, { glyph: string; cls: string }> = {
    completed: { glyph: '✓', cls: 'ok' },
    done: { glyph: '✓', cls: 'ok' },
    failed: { glyph: '✕', cls: 'fail' },
    running: { glyph: '', cls: 'run' },
    interrupted: { glyph: '⊘', cls: 'interrupted' },
  };
  const s = map[value] ?? { glyph: '○', cls: 'idle' };
  return <span className={`status-icon ${s.cls}`} title={title ?? value} aria-label={value}>{s.glyph}</span>;
}

// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, '').replace(/\x1b[()][AB0-2]/g, '');
}

function formatTime(value: string | undefined) {
  if (!value) return '';
  const d = new Date(value);
  return Number.isNaN(d.valueOf()) ? '' : d.toLocaleTimeString([], { hour12: false });
}

function formatDuration(start?: string, end?: string): string {
  if (!start) return '';
  if (!end) return 'running';
  const secs = Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000);
  if (secs < 60) return `${secs}s`;
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function formatDateTime(value: string | undefined): string {
  if (!value) return '';
  const d = new Date(value);
  return Number.isNaN(d.valueOf()) ? value : d.toLocaleString([], { hour12: false });
}

function Status({ value, label }: { value: string; label?: string }) {
  return <span className={`status status-${value}`}>{label ?? value}</span>;
}

function EmptyRow({ colSpan, label }: { colSpan: number; label: string }) {
  return <tr><td colSpan={colSpan}><span className="empty">{label}</span></td></tr>;
}

function compareInboxItems(a: any, b: any) {
  const aTime = Date.parse(a.created_at || a.updated_at || '') || 0;
  const bTime = Date.parse(b.created_at || b.updated_at || '') || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(a.id).localeCompare(String(b.id));
}

function inboxGate(labels: string[]) {
  if (labels.includes(ARCHON_ACCEPT)) return 'accept';
  if (labels.includes(ARCHON_PENDING)) return 'pending';
  if (labels.includes(ARCHON_REJECTED)) return 'reject';
  return 'clear';
}

function gateLabel(gate: string) {
  if (gate === 'accept') return 'agent-ready';
  if (gate === 'pending') return 'not-ready';
  if (gate === 'reject') return 'rejected';
  return 'unlabeled';
}

function inboxComments(item: any) {
  const comments = item.metadata?.comments;
  if (Array.isArray(comments)) return comments;
  if (Array.isArray(comments?.nodes)) return comments.nodes;
  return [];
}

function inboxAuthor(item: any) {
  for (const value of [item.author, item.metadata?.author]) {
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (value?.login) return String(value.login).trim();
  }
  return '';
}

function inboxTitleAndBody(item: any) {
  const text = String(item.body ?? '').trim();
  if (!text) return { title: item.id, body: '' };
  const [first, ...rest] = text.split(/\n+/);
  return {
    title: first.trim() || item.id,
    body: rest.join('\n').trim(),
  };
}

function inboxSourceUrl(item: any, repo?: string | null) {
  if (typeof item.metadata?.url === 'string' && item.metadata.url) return item.metadata.url;
  if (!repo || !item.source_ref) return '';
  const [kind, number] = String(item.source_ref).split(':');
  if (!number) return '';
  if (kind === 'issue') return `https://github.com/${repo}/issues/${number}`;
  if (kind === 'pr') return `https://github.com/${repo}/pull/${number}`;
  return '';
}

function matchesInboxFilters(
  item: any,
  filters: { query: string; providerFilter: string; statusFilter: string; gateFilter: string },
) {
  if (filters.providerFilter !== 'all' && item.provider !== filters.providerFilter) return false;
  if (filters.statusFilter !== 'all' && item.status !== filters.statusFilter) return false;
  const gate = inboxGate(item.labels ?? []);
  if (filters.gateFilter !== 'all' && gate !== filters.gateFilter) return false;
  const query = filters.query.trim().toLowerCase();
  if (!query) return true;
  const comments = inboxComments(item).map((comment: any) => comment.body ?? '').join(' ');
  const haystack = [
    item.id,
    item.provider,
    item.kind,
    item.status,
    item.body,
    inboxAuthor(item),
    item.source_ref,
    ...(item.labels ?? []),
    comments,
  ].join(' ').toLowerCase();
  return haystack.includes(query);
}

function formatDate(value: string | undefined) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

function firstSessionRef(sessions: any[]): string {
  for (const session of sessions) {
    if (session.ref) return session.ref;
    const child = firstSessionRef(session.children ?? []);
    if (child) return child;
  }
  return '';
}

// `tokens_in` reported by codex includes cached re-reads (often 90%+), so the
// headline "in" is the NET (non-cached) input; cached is shown separately.
function netTokensIn(usage: any): number {
  return Math.max((usage?.tokens_in ?? 0) - (usage?.cached_tokens_in ?? 0), 0);
}

function formatUsageBrief(usage: any) {
  if (!usage) return 'no usage';
  const tokensIn = netTokensIn(usage);
  const tokensOut = usage.tokens_out ?? 0;
  const cost = typeof usage.cost_usd === 'number' ? ` / $${usage.cost_usd.toFixed(4)}` : '';
  return ((usage.tokens_in ?? 0) || tokensOut)
    ? `${tokensIn.toLocaleString()} in / ${tokensOut.toLocaleString()} out${cost}`
    : cost ? cost.slice(3) : 'no usage';
}

function formatUsage(usage: any) {
  if (!usage) return '';
  const cached = usage.cached_tokens_in ? ` (+${(usage.cached_tokens_in).toLocaleString()} cached)` : '';
  const reasoning = usage.reasoning_tokens_out ? ` (${usage.reasoning_tokens_out} reasoning)` : '';
  const cost = typeof usage.cost_usd === 'number' ? ` / $${usage.cost_usd.toFixed(4)}` : '';
  return `${netTokensIn(usage).toLocaleString()} tokens in${cached} / ${(usage.tokens_out ?? 0).toLocaleString()} tokens out${reasoning}${cost}`;
}
