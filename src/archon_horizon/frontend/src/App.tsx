import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, Navigate, NavLink, Route, Routes, useSearchParams } from 'react-router-dom';
import { editInbox, editRoadmap, editTask, getState, getBlueprints, getProjects, getProjectMetrics, getReport, getTranscriptPage, getTranscripts, getSessionFileDiff, getSessionCommits, searchDeclarations, getBlueprintChapters, type ProjectStat, type ProjectMetrics, type SessionChangeFile, type CommitChange, type FileDiff, type SessionInboxActivity, type SessionRoadmapActivity } from './api';
import { isStaticDashboard } from './staticMode';
import { version as APP_VERSION } from '../package.json';
import MarkdownBlock, { markdownToHtml } from './components/MarkdownBlock';
import BlueprintRendered from './components/BlueprintRendered';
import LeanCodeLine from './components/LeanCodeLine';
import { useProgressiveCount } from './hooks/useProgressiveCount';
import BlueprintPage from './BlueprintPage';
import DagPage from './DagPage';
import LeanPage from './LeanPage';
import BoardPage from './BoardPage';
import { RefLinkProvider, useRefResolver, useRefLinks, refChipClickHandler, inboxOwnerTask, inboxReadBy, RefChip } from './refs';

const STATIC = isStaticDashboard();
// Triage labels (must match core/labels.py). The UI gate vocabulary stays
// accept/pending/reject; these are the on-disk label strings they map to.
const ARCHON_ACCEPT = 'agent-ready';
const ARCHON_PENDING = 'not-ready';
const ARCHON_REJECTED = 'rejected';
const INBOX_KIND_OPTIONS = ['conversation', 'hint', 'issue', 'protection', 'info', 'memory'];
const INBOX_PROVIDER_OPTIONS = ['local', 'github'];
const INBOX_STATUS_OPTIONS = ['open', 'closed', 'archived'];
// Resolved and archived items remain available, but triage starts with live work.
const INBOX_STATUS_DEFAULT = ['open'];
const INBOX_GATE_OPTIONS = ['accept', 'pending', 'reject', 'clear'];
const INBOX_WORKSPACE_SCOPE = '__workspace__';
const TASK_STATUS_OPTIONS = ['queued', 'running', 'blocked', 'done', 'failed', 'cancelled'];
const TASK_PRIORITY_OPTIONS = ['urgent', 'high', 'normal', 'low'];
const ROADMAP_STATUS_OPTIONS = ['active', 'pending', 'blocked', 'done', 'rejected'];
const ROADMAP_KIND_OPTIONS = ['proof', 'blueprint', 'refactor', 'workspace', 'report'];

type HorizonState = {
  workspace: string;
  workspace_root?: string;
  config_dir?: string;
  projects?: string[];
  roadmap?: { items?: any[] };
  roadmap_warnings?: string[];
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
// error message (e.g. a graph renderer exception) instead of a white screen.
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
      Cannot reach server - check that <code>horizon dashboard</code> is running on the current port.
    </div>
  );
}

export function App() {
  const [state, setState] = useState<HorizonState | null>(null);
  const [isError, setIsError] = useState(false);

  const reload = () => {
    // Blueprints ride a separate endpoint (they change only on publish/sync;
    // its ETag makes the poll a 304), merged back so consumers still read
    // `state.blueprints`. A blueprint fetch failure never blocks the state.
    getState()
      .then((next) => {
        setState((current) => (
          next && next.blueprints === undefined && current?.blueprints
            ? { ...next, blueprints: current.blueprints }
            : next
        ));
        setIsError(false);
        // Do not make the dashboard shell wait for blueprint data. Its own ETag
        // keeps later polls cheap, and blueprint consumers update when it lands.
        getBlueprints()
          .then((blueprints) => setState((current) => (
            current ? { ...current, blueprints } : current
          )))
          .catch(() => undefined);
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

  return <AppShell state={state} reload={reload} isError={isError} />;
}

// Split out so the ref-link resolver (and its react-router `navigate`) can live
// inside a component that always has a non-null state.
function AppShell({ state, reload, isError }: { state: HorizonState; reload: () => void; isError: boolean }) {
  const resolve = useRefResolver(state);
  return (
    <RefLinkProvider resolve={resolve}>
    <div className="app">
      <ConnectionBanner isError={isError} />
      <header className="header">
        <h1>Archon Horizon</h1>
        <span className="version-badge" title={`Horizon dashboard v${APP_VERSION}`}>v{APP_VERSION}</span>
        <span className="project-badge" title={state.workspace_root || state.workspace}>{state.workspace}</span>
        {STATIC && <span className="project-badge" title={window.__ARCHON_STATIC__?.generatedAt}>static</span>}
        <nav className="header-nav" aria-label="Dashboard">
          <NavLink to="/" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`} end>Overview</NavLink>
          <NavLink to="/inbox" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Inbox</NavLink>
          <NavLink to="/roadmap" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Roadmap</NavLink>
          <NavLink to="/tasks" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Tasks</NavLink>
          <NavLink to="/board" className={({ isActive }) => `nav-link ${isActive ? 'active' : ''}`}>Board</NavLink>
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
          <Route path="/board" element={<BoardPage state={state} reload={reload} />} />
          <Route path="/search" element={<SearchPage state={state} />} />
          <Route path="/blueprint" element={<BlueprintPage state={state} />} />
          <Route path="/dag" element={<DagPage state={state} reload={reload} />} />
          <Route path="/logs" element={<Transcripts state={state} />} />
          <Route path="/transcripts" element={<Navigate to="/logs" replace />} />
          <Route path="/lean" element={<LeanPage state={state} />} />
          <Route path="/code" element={<Navigate to="/lean" replace />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </ErrorBoundary>
      </main>
    </div>
    </RefLinkProvider>
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
  const recentRuns = [...runs].sort(compareRuns).slice(0, 5);

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

      <ProjectsPanel projectNames={state.projects ?? []} />

      <Panel title="Recent Runs" subtitle="Run logs, rounds, sessions, and transcript links" to="/logs">
        <RunList runs={recentRuns} />
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

function ProjectsPanel({ projectNames }: { projectNames: string[] }) {
  const [indexProjects, setIndexProjects] = useState<ProjectStat[]>([]);
  const [metrics, setMetrics] = useState<Record<string, ProjectMetrics | false>>({});
  const projectKey = projectNames.join('\u0000');
  const projects = useMemo(() => {
    const indexed = new Map(indexProjects.map((project) => [project.name, project]));
    const names = [...new Set([...projectNames, ...indexProjects.map((project) => project.name)])];
    return names.map((name) => indexed.get(name) ?? { name, depends_on: [] });
  }, [projectKey, indexProjects]);
  const metricsKey = projects.map((project) => project.name).join('\u0000');

  useEffect(() => {
    let cancelled = false;
    getProjects()
      .then((data) => { if (!cancelled) setIndexProjects(data.projects ?? []); })
      .catch(() => undefined);
    return () => { cancelled = true; };
  }, [projectKey]);

  useEffect(() => {
    let cancelled = false;
    let cursor = 0;
    setMetrics({});
    const loadWorker = async () => {
      while (!cancelled && cursor < projects.length) {
        const project = projects[cursor++];
        try {
          const value = await getProjectMetrics(project.name);
          if (!cancelled) setMetrics((current) => ({ ...current, [project.name]: value }));
        } catch {
          if (!cancelled) setMetrics((current) => ({ ...current, [project.name]: false }));
        }
      }
    };
    void loadWorker();
    void loadWorker();
    return () => { cancelled = true; };
  }, [metricsKey]);

  if (projects.length === 0) return null;
  const metricCell = (metric: ProjectMetrics | false | undefined, field: keyof Omit<ProjectMetrics, 'name'>) => {
    if (metric === false) return <span className="empty">Unavailable</span>;
    if (!metric) return <span className="project-metric-loading">Loading...</span>;
    return metric[field].toLocaleString();
  };
  return (
    <Panel title="Workspace" subtitle="Per-project Lean metrics load independently">
      <div className="table-wrap">
        <table className="projects-table">
          <thead>
            <tr>
              <th>Project</th><th>Lean files</th><th>LOC</th><th>Code</th><th>Open sorries</th>
            </tr>
          </thead>
          <tbody>
            {projects.map((project) => {
              const metric = metrics[project.name];
              return <tr key={project.name}>
                <td>
                  <strong>{project.name}</strong>
                  {(project.depends_on ?? []).length > 0 && (
                    <div className="project-deps">
                      depends on {(project.depends_on ?? []).join(', ')}
                    </div>
                  )}
                </td>
                <td>{metricCell(metric, 'lean_files')}</td>
                <td>{metricCell(metric, 'loc')}</td>
                <td>{metricCell(metric, 'loc_code')}</td>
                <td>{metric && metric.sorries > 0
                  ? <span className="sorry-pill">{metric.sorries.toLocaleString()}</span>
                  : metric
                    ? <span className="ok-pill">0</span>
                    : metricCell(metric, 'sorries')}</td>
              </tr>;
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export function chipTone(value: string) {
  const normalized = filterToken(value);
  if (INBOX_KIND_OPTIONS.includes(value)) return `kind-${normalized}`;
  if (SEARCH_KINDS.includes(value)) return `kind-${normalized}`;
  if (INBOX_PROVIDER_OPTIONS.includes(value)) return `provider-${normalized}`;
  if (INBOX_GATE_OPTIONS.includes(value)) return `gate-${normalized}`;
  if (TASK_PRIORITY_OPTIONS.includes(value)) return `priority-${normalized}`;
  if ([...TASK_STATUS_OPTIONS, ...ROADMAP_STATUS_OPTIONS, ...INBOX_STATUS_OPTIONS, 'completed', 'closed'].includes(value)) {
    return `state-${normalized}`;
  }
  return 'project';
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
        <label key={p} className={`project-chip tag-chip tag-project ${value.includes(p) ? 'on' : ''}`}>
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
  const [searchParams] = useSearchParams();
  const focusedTaskId = searchParams.get('task') ?? '';
  const roadmapIds = (state.roadmap?.items ?? []).map((item: any) => item.id);
  const projects = state.projects ?? [];
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const taskProjectOptions = useMemo(() => {
    const all = new Set<string>(projects);
    for (const task of tasks) {
      for (const project of task.projects ?? task.write_set?.projects ?? (task.project ? [task.project] : [])) {
        if (project) all.add(project);
      }
    }
    return [...all].sort();
  }, [projects, tasks]);
  const [statusFilter, toggleStatusFilter] = useFilterSelection(TASK_STATUS_OPTIONS);
  const [priorityFilter, togglePriorityFilter] = useFilterSelection(TASK_PRIORITY_OPTIONS);
  const [projectFilter, toggleProjectFilter] = useFilterSelection(taskProjectOptions);

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

  const focusedTask = focusedTaskId ? tasks.find((task: any) => task.id === focusedTaskId) : null;
  const filteredTasksBase = tasks
    .filter((task: any) => statusFilter.has(task.status || 'queued'))
    .filter((task: any) => priorityFilter.has(task.priority || 'normal'))
    .filter((task: any) => {
      if (taskProjectOptions.length === 0) return true;
      const taskProjects: string[] = task.projects ?? task.write_set?.projects ?? (task.project ? [task.project] : []);
      return taskProjects.some((project) => projectFilter.has(project));
    });
  const filteredTasks = (
    focusedTask && !filteredTasksBase.some((task: any) => task.id === focusedTaskId)
      ? [focusedTask, ...filteredTasksBase]
      : filteredTasksBase
  ).sort(compareTasks);
  const taskGroups = groupByDay(filteredTasks, taskActivityAt);

  return (
    <div className="page">
      <Panel title="Tasks" subtitle={`${filteredTasks.length}/${tasks.length} task${tasks.length === 1 ? '' : 's'}`}>
        {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
        {!STATIC && (
          <details className="local-create">
            <summary>New task</summary>
            <TaskComposer runAction={runAction} projects={projects} roadmapIds={roadmapIds} />
          </details>
        )}
        <div className="filter-stack">
          <div className="search-facet-group"><span className="search-facet-label">Status</span>
            <ChipMultiSelect options={TASK_STATUS_OPTIONS} selected={statusFilter} onToggle={toggleStatusFilter} label="Task status filter" />
          </div>
          <div className="search-facet-group"><span className="search-facet-label">Priority</span>
            <ChipMultiSelect options={TASK_PRIORITY_OPTIONS} selected={priorityFilter} onToggle={togglePriorityFilter} label="Task priority filter" />
          </div>
          {taskProjectOptions.length > 0 && (
            <div className="search-facet-group"><span className="search-facet-label">Projects</span>
              <ChipMultiSelect options={taskProjectOptions} selected={projectFilter} onToggle={toggleProjectFilter} label="Task project filter" />
            </div>
          )}
        </div>
        <div className="task-list">
          {taskGroups.map((group) => (
            <section key={group.key} className="day-group">
              <div className="day-heading"><h3>{group.label}</h3><span>{group.items.length}</span></div>
              {group.items.map((task: any) => (
                <TaskCard key={task.id} task={task} runAction={runAction} projects={projects} roadmapIds={roadmapIds} focused={task.id === focusedTaskId} />
              ))}
            </section>
          ))}
          {filteredTasks.length === 0 && <p className="empty">No tasks match the current filters.</p>}
        </div>
      </Panel>
    </div>
  );
}

function splitList(value: string) {
  return value.split(',').map((part) => part.trim()).filter(Boolean);
}

function useFilterSelection(options: string[], initialSelected?: string[]) {
  const sig = options.join('\0');
  const previousOptions = useRef<string[]>(options);
  // Default to all options selected, unless an explicit initial subset is given
  // (e.g. inbox: hide 'archived' by default until the user opts in).
  const [selected, setSelected] = useState<Set<string>>(() => new Set(initialSelected ?? options));

  useEffect(() => {
    setSelected((prev) => {
      const oldOptions = previousOptions.current;
      const oldSet = new Set(oldOptions);
      const allowed = new Set(options);
      const wasAllSelected = oldOptions.length === 0 || oldOptions.every((option) => prev.has(option));
      const next = new Set([...prev].filter((option) => allowed.has(option)));
      for (const option of options) {
        if (!oldSet.has(option) || wasAllSelected) next.add(option);
      }
      previousOptions.current = options;
      return next;
    });
  }, [sig]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (value: string) => setSelected((prev) => {
    const next = new Set(prev);
    next.has(value) ? next.delete(value) : next.add(value);
    return next;
  });

  return [selected, toggle] as const;
}

function dayStamp(value: string | undefined) {
  if (!value) return { key: 'undated', label: 'Undated' };
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return { key: 'undated', label: 'Undated' };
  const key = [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, '0'),
    String(date.getDate()).padStart(2, '0'),
  ].join('-');
  return {
    key,
    label: date.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }),
  };
}

function groupByDay<T>(items: T[], getDate: (item: T) => string | undefined) {
  const groups: { key: string; label: string; items: T[] }[] = [];
  const byKey = new Map<string, { key: string; label: string; items: T[] }>();
  for (const item of items) {
    const stamp = dayStamp(getDate(item));
    let group = byKey.get(stamp.key);
    if (!group) {
      group = { ...stamp, items: [] };
      byKey.set(stamp.key, group);
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
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

function TaskCard({ task, runAction, projects = [], roadmapIds = [], focused = false }: { task: any; runAction?: any; projects?: string[]; roadmapIds?: string[]; focused?: boolean }) {
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
    <details className={`task-card${focused ? ' focused' : ''}`} open={editing || focused}>
      <summary style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div className="task-title" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: 1, minWidth: 0 }}>
          {!STATIC && runAction ? (
            <select className={`chip-select state ${status}`} value={status} onChange={(e) => updateStatus(e.target.value)}>
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
        <button
          key={opt}
          type="button"
          className={`search-facet-chip tag-chip tag-${chipTone(opt)} ${selected.has(opt) ? 'on' : ''}`}
          onClick={() => onToggle(opt)}
        >
          {filterLabel(opt)}
        </button>
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

// Hierarchy is stored in item.metadata (parent id and/or a depth level), mirroring
// core/roadmap.py's ordered_tree — so the dashboard renders the same outline the
// CLI does: parents above their sub-items, ordered by id, one row per item.
function roadmapDepth(item: any): number {
  const d = Number(item?.metadata?.depth);
  return Number.isFinite(d) && d > 0 ? Math.floor(d) : 0;
}
function roadmapParent(item: any): string {
  const p = item?.metadata?.parent;
  return typeof p === 'string' ? p.trim() : '';
}
function orderedRoadmapTree(items: any[]): Array<{ item: any; depth: number }> {
  const byId = new Map(items.map((it) => [it.id, it]));
  const children = new Map<string, any[]>();
  const roots: any[] = [];
  for (const it of items) {
    const parent = roadmapParent(it);
    if (parent && byId.has(parent) && parent !== it.id) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent)!.push(it);
    } else roots.push(it);
  }
  const byId3 = (a: any, b: any) => String(a.id).localeCompare(String(b.id));
  for (const kids of children.values()) kids.sort(byId3);
  roots.sort(byId3);
  const out: Array<{ item: any; depth: number }> = [];
  const seen = new Set<string>();
  const walk = (node: any, depth: number) => {
    if (seen.has(node.id)) return;
    seen.add(node.id);
    out.push({ item: node, depth });
    for (const child of children.get(node.id) ?? []) walk(child, depth + 1);
  };
  for (const root of roots) walk(root, roadmapDepth(root));
  for (const it of items) if (!seen.has(it.id)) { seen.add(it.id); out.push({ item: it, depth: roadmapDepth(it) }); }
  return out;
}

// Prune the ordered tree rows to what an expand/collapse state shows: a
// collapsed item keeps its own row but hides every following row that is
// deeper (its subtree). Also annotates each row with whether it has children
// in THIS row list (post-filtering) and how many rows its subtree holds, so
// the toggle can label what it hides.
function visibleRoadmapRows(
  rows: Array<{ item: any; depth: number }>,
  collapsed: Set<string>,
): Array<{ item: any; depth: number; hasChildren: boolean; subtreeSize: number }> {
  const out: Array<{ item: any; depth: number; hasChildren: boolean; subtreeSize: number }> = [];
  let skipDepth: number | null = null;
  rows.forEach((row, i) => {
    if (skipDepth !== null) {
      if (row.depth > skipDepth) return;
      skipDepth = null;
    }
    let subtreeSize = 0;
    while (i + 1 + subtreeSize < rows.length && rows[i + 1 + subtreeSize].depth > row.depth) subtreeSize++;
    out.push({ ...row, hasChildren: subtreeSize > 0, subtreeSize });
    if (collapsed.has(row.item.id)) skipDepth = row.depth;
  });
  return out;
}

function RoadmapPage({ state, reload }: PageProps) {
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const [creating, setCreating] = useState(false);
  const items = state.roadmap?.items ?? [];
  const allProjects = state.projects || [];

  const roadmapProjectOptions = useMemo(() => {
    const all = new Set<string>(allProjects);
    for (const item of items) {
      for (const project of (item.projects?.length ? item.projects : ['Uncategorized'])) all.add(project);
    }
    return [...all].sort();
  }, [allProjects, items]);
  const [projectFilter, toggleProjectFilter] = useFilterSelection(roadmapProjectOptions);
  const [statusFilter, toggleStatusFilter] = useFilterSelection(ROADMAP_STATUS_OPTIONS);

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
    if (!itemProjects(i).some((project) => projectFilter.has(project))) return false;
    if (!statusFilter.has(i.status || 'active')) return false;
    return true;
  });
  // An item shared across projects appears under each of its project groups.
  const filteredProjects = [...new Set(filteredItems.flatMap(itemProjects))].sort();
  // Tree order (parents above sub-items) computed once over the visible items.
  const orderedFiltered = orderedRoadmapTree(filteredItems);

  // Expand/collapse of sub-item trees. Ids of items that have children in the
  // filtered set — the targets of "Collapse all".
  const [collapsedIds, setCollapsedIds] = useState<Set<string>>(new Set());
  const parentIds = useMemo(() => {
    const byId = new Set(filteredItems.map((i: any) => i.id));
    const ids = new Set<string>();
    for (const it of filteredItems) {
      const parent = roadmapParent(it);
      if (parent && parent !== it.id && byId.has(parent)) ids.add(parent);
    }
    return ids;
  }, [filteredItems]);
  const toggleCollapsed = (id: string) => setCollapsedIds((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <div className="page">
      <Panel title="Roadmap" subtitle={`${filteredItems.length} item${filteredItems.length === 1 ? '' : 's'}`}>
        {STATIC && <div className="notice info">This roadmap snapshot is read-only. Editing is available in the live dashboard.</div>}
        <div className="filter-stack">
          {roadmapProjectOptions.length > 0 && (
            <div className="search-facet-group"><span className="search-facet-label">Projects</span>
              <ChipMultiSelect options={roadmapProjectOptions} selected={projectFilter} onToggle={toggleProjectFilter} label="Roadmap project filter" />
            </div>
          )}
          <div className="search-facet-group"><span className="search-facet-label">Status</span>
            <ChipMultiSelect options={ROADMAP_STATUS_OPTIONS} selected={statusFilter} onToggle={toggleStatusFilter} label="Roadmap status filter" />
            {!STATIC && <button className="primary roadmap-add-inline" type="button" onClick={() => setCreating((open) => !open)}>
              {creating ? 'Close editor' : '+ Add item'}
            </button>}
          </div>
          {parentIds.size > 0 && (
            <div className="search-facet-group"><span className="search-facet-label">Tree</span>
              <button className="text-action" onClick={() => setCollapsedIds(new Set(parentIds))}>Collapse all</button>
              <button className="text-action" onClick={() => setCollapsedIds(new Set())}>Expand all</button>
            </div>
          )}
        </div>
        {!STATIC && creating && (
          <div className="roadmap-editor-shell">
            <RoadmapEditor
              runAction={runAction}
              projects={allProjects}
              parentOptions={items.map((i: any) => i.id)}
              onCancel={() => setCreating(false)}
              onSaved={() => setCreating(false)}
            />
          </div>
        )}
        {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
        {(state.roadmap_warnings ?? []).map((w: string) => (
          // Parent/child status inconsistencies — informational only; the CLI/agent
          // decides whether to correct them (they may be intentional).
          <div key={w} className="notice error" style={{ opacity: 0.85 }}>⚠ {w}</div>
        ))}
        <div className="roadmap-list" style={{ display: 'flex', flexDirection: 'column', gap: '1rem' }}>
          {filteredProjects.map((proj) => (
            <div key={proj as string} className="roadmap-project-group">
              <h3>{proj}</h3>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {visibleRoadmapRows(
                  orderedFiltered.filter(({ item }) => itemProjects(item).includes(proj)),
                  collapsedIds,
                ).map(({ item, depth, hasChildren, subtreeSize }) => (
                  <div key={item.id} style={{ marginLeft: `${Math.min(depth, 6) * 1.5}rem`, display: 'flex', alignItems: 'flex-start', gap: '0.35rem' }}>
                    {hasChildren ? (
                      <button
                        className="tree-toggle button-reset"
                        onClick={() => toggleCollapsed(item.id)}
                        aria-expanded={!collapsedIds.has(item.id)}
                        title={collapsedIds.has(item.id)
                          ? `Show ${subtreeSize} sub-item${subtreeSize === 1 ? '' : 's'}`
                          : `Hide ${subtreeSize} sub-item${subtreeSize === 1 ? '' : 's'}`}
                      >
                        {collapsedIds.has(item.id) ? '▸' : '▾'}
                        {collapsedIds.has(item.id) && <span className="tree-toggle-count">{subtreeSize}</span>}
                      </button>
                    ) : (
                      <span className="tree-toggle-spacer" aria-hidden="true" />
                    )}
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <RoadmapItemCard
                        item={item}
                        runAction={runAction}
                        projects={allProjects}
                        parentOptions={items.map((candidate: any) => candidate.id)}
                      />
                    </div>
                  </div>
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

export function RoadmapEditor({
  item,
  runAction,
  projects,
  parentOptions = [],
  initialMilestone = '',
  requireMilestone = false,
  onCancel,
  onSaved,
}: {
  item?: any;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
  projects: string[];
  parentOptions?: string[];
  initialMilestone?: string;
  requireMilestone?: boolean;
  onCancel: () => void;
  onSaved: () => void;
}) {
  const editing = !!item;
  const [itemId, setItemId] = useState('');
  const [title, setTitle] = useState(item?.title ?? '');
  const [summary, setSummary] = useState(item?.summary ?? '');
  const [selProjects, setSelProjects] = useState<string[]>(item?.projects ?? []);
  const [author, setAuthor] = useState('human');
  const [status, setStatus] = useState(item?.status ?? 'pending');
  const [kind, setKind] = useState(item?.kind ?? 'proof');
  const [priority, setPriority] = useState(item?.priority ?? 'normal');
  const [parent, setParent] = useState(roadmapParent(item));
  const [depth, setDepth] = useState(item?.metadata?.depth === undefined ? '' : String(item.metadata.depth));
  const [owner, setOwner] = useState(item?.metadata?.owner ?? '');
  const [milestone, setMilestone] = useState(item?.metadata?.milestone ?? initialMilestone);
  const [dependsOn, setDependsOn] = useState((item?.depends_on ?? []).join(', '));
  const [inboxRefs, setInboxRefs] = useState((item?.inbox_refs ?? []).join(', '));
  const [taskRefs, setTaskRefs] = useState((item?.task_refs ?? []).join(', '));
  const [pinnedCommits, setPinnedCommits] = useState((item?.metadata?.pinned_commits ?? []).join(', '));
  const [busy, setBusy] = useState(false);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (
      !title.trim()
      || !author.trim()
      || selProjects.length === 0
      || (requireMilestone && !milestone.trim())
      || busy
    ) return;
    setBusy(true);
    runAction({
      action: editing ? 'edit' : 'add',
      ...(editing ? { id: item.id } : itemId.trim() ? { id: itemId.trim() } : {}),
      title: title.trim(),
      summary: summary.trim(),
      projects: selProjects,
      author: author.trim(),
      status,
      kind,
      priority,
      parent: parent.trim(),
      ...(depth.trim() ? { depth: Number(depth) } : {}),
      owner: owner.trim(),
      milestone: milestone.trim(),
      depends_on: splitList(dependsOn),
      inbox_refs: splitList(inboxRefs),
      task_refs: splitList(taskRefs),
      pinned_commits: splitList(pinnedCommits),
    }, editing ? 'Roadmap item updated.' : 'Roadmap item added.')
      .then((ok) => {
        if (!ok) return;
        onSaved();
      })
      .finally(() => setBusy(false));
  };

  return (
    <form className="roadmap-editor" onSubmit={submit}>
      <div className="roadmap-editor-grid">
        {!editing && <label>Item ID <input value={itemId} placeholder="Generated when blank" onChange={(event) => setItemId(event.target.value)} /></label>}
        <label className={editing ? 'roadmap-editor-wide' : ''}>Title <input autoFocus value={title} onChange={(event) => setTitle(event.target.value)} required /></label>
        <label>Changed by <input value={author} onChange={(event) => setAuthor(event.target.value)} required /></label>
        <label>Status <select value={status} onChange={(event) => setStatus(event.target.value)}>{ROADMAP_STATUS_OPTIONS.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Kind <select value={kind} onChange={(event) => setKind(event.target.value)}>{ROADMAP_KIND_OPTIONS.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Priority <select value={priority} onChange={(event) => setPriority(event.target.value)}>{TASK_PRIORITY_OPTIONS.map((value) => <option key={value}>{value}</option>)}</select></label>
        <label>Parent <input value={parent} list={`roadmap-parent-${item?.id ?? 'new'}`} onChange={(event) => setParent(event.target.value)} /></label>
        <datalist id={`roadmap-parent-${item?.id ?? 'new'}`}>{parentOptions.filter((id) => id !== item?.id).map((id) => <option key={id} value={id} />)}</datalist>
        <label>Owner <input value={owner} onChange={(event) => setOwner(event.target.value)} /></label>
        <label>Milestone <input value={milestone} onChange={(event) => setMilestone(event.target.value)} /></label>
        <label>Fallback depth <input type="number" min="0" value={depth} onChange={(event) => setDepth(event.target.value)} /></label>
      </div>
      <fieldset className="roadmap-project-field">
        <legend>Projects</legend>
        <MultiProjectSelect value={selProjects} onChange={setSelProjects} projects={projects} />
      </fieldset>
      <label className="roadmap-editor-summary">Summary <textarea value={summary} onChange={(event) => setSummary(event.target.value)} rows={5} /></label>
      <details className="roadmap-editor-advanced">
        <summary>Links and dependencies</summary>
        <div className="roadmap-editor-grid">
          <label>Depends on <input value={dependsOn} onChange={(event) => setDependsOn(event.target.value)} placeholder="Comma-separated item IDs" /></label>
          <label>Task refs <input value={taskRefs} onChange={(event) => setTaskRefs(event.target.value)} placeholder="Comma-separated task IDs" /></label>
          <label>Inbox refs <input value={inboxRefs} onChange={(event) => setInboxRefs(event.target.value)} placeholder="Comma-separated inbox IDs" /></label>
          <label>Pinned commits <input value={pinnedCommits} onChange={(event) => setPinnedCommits(event.target.value)} placeholder="Comma-separated SHAs" /></label>
        </div>
      </details>
      {selProjects.length === 0 && <span className="roadmap-editor-error">Select at least one project.</span>}
      {requireMilestone && !milestone.trim() && <span className="roadmap-editor-error">Set a milestone for this board item.</span>}
      <div className="roadmap-editor-actions">
        <button type="button" onClick={onCancel}>Cancel</button>
        <button className="primary" type="submit" disabled={!title.trim() || !author.trim() || selProjects.length === 0 || (requireMilestone && !milestone.trim()) || busy}>{busy ? 'Saving...' : editing ? 'Save changes' : 'Create item'}</button>
      </div>
    </form>
  );
}


function InboxPage({ state, reload }: PageProps) {
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const [query, setQuery] = useState('');
  const [providerFilter, toggleProviderFilter] = useFilterSelection(INBOX_PROVIDER_OPTIONS);
  const [statusFilter, toggleStatusFilter] = useFilterSelection(INBOX_STATUS_OPTIONS, INBOX_STATUS_DEFAULT);
  const [gateFilter, toggleGateFilter] = useFilterSelection(INBOX_GATE_OPTIONS);
  const [kindFilter, toggleKindFilter] = useFilterSelection(INBOX_KIND_OPTIONS);
  const providers = state.inbox_providers ?? {};
  const github = providers.github;
  const allItems = [...(state.local_inbox ?? []), ...(state.github_inbox ?? [])];
  const runningTeams = useMemo(() => activeInboxTeams(state), [state.runs, state.tasks]);
  const conversationDetails = useRef<HTMLDetailsElement>(null);
  const [conversationTarget, setConversationTarget] = useState<string[]>([]);
  const [conversationSeed, setConversationSeed] = useState(0);
  const audienceOptions = useMemo(() => {
    const values = new Set(allItems.flatMap((item) => {
      const targets = inboxAudienceTargets(item);
      return targets.length ? targets : ['general'];
    }));
    return ['general', ...[...values].filter((value) => value !== 'general').sort()];
  }, [allItems]);
  const ownerOptions = useMemo(() => {
    const values = new Set(allItems.map((item) => inboxOwnerTask(item) || 'shared'));
    return ['shared', ...[...values].filter((value) => value !== 'shared').sort()];
  }, [allItems]);
  const projectOptions = useMemo(
    () => {
      const scoped = [...new Set(allItems.flatMap((item) => scopeTargets(item, 'projects')))].sort();
      const hasWorkspaceItems = allItems.some((item) => scopeTargets(item, 'projects').length === 0);
      return hasWorkspaceItems ? [INBOX_WORKSPACE_SCOPE, ...scoped] : scoped;
    },
    [allItems],
  );
  const authorOptions = useMemo(
    () => [...new Set(allItems.map(inboxAuthor).filter(Boolean))].sort(),
    [allItems],
  );
  const [audienceFilter, toggleAudienceFilter] = useFilterSelection(audienceOptions);
  const [ownerFilter, toggleOwnerFilter] = useFilterSelection(ownerOptions);
  const [projectFilter, toggleProjectFilter] = useFilterSelection(projectOptions);
  const [authorFilter, toggleAuthorFilter] = useFilterSelection(authorOptions);
  const items = allItems
    .filter((item) => matchesInboxFilters(item, {
      query,
      providerFilter,
      statusFilter,
      gateFilter,
      kinds: kindFilter,
      audienceFilter,
      ownerFilter,
      projectFilter,
      authorFilter,
    }))
    .sort(compareInboxItems);
  const itemGroups = groupByDay(items, inboxActivityAt);
  const conversationRecipients = useMemo<InboxRecipientOption[]>(() => {
    const options: InboxRecipientOption[] = [
      { value: 'human', label: 'Human' },
      { value: 'horizon', label: 'All Horizon teams' },
      ...runningTeams.map((team) => ({
        value: team.recipient,
        label: team.taskTitle || team.task || `Run ${team.run}`,
        detail: `run ${team.run}${team.task ? ` · ${team.task}` : ''}`,
      })),
      ...(state.projects ?? []).map((project) => ({
        value: `project:${project}`,
        label: project,
        detail: 'project',
      })),
    ];
    return options.filter((option, index) => (
      options.findIndex((candidate) => candidate.value === option.value) === index
    ));
  }, [runningTeams, state.projects]);

  const startConversation = (recipient?: string) => {
    setConversationTarget(recipient ? [recipient] : []);
    setConversationSeed((value) => value + 1);
    requestAnimationFrame(() => {
      if (conversationDetails.current) {
        conversationDetails.current.open = true;
        conversationDetails.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    });
  };

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
      <Panel title="Inbox" subtitle="Search, review, and archive inbox items">
        <div className="inbox-topbar">
          <div className="inbox-searchbar">
            <input value={query} placeholder="Search inbox..." onChange={(event) => setQuery(event.target.value)} />
          </div>
          <div className="inbox-filterbar">
            <div className="search-facet-group"><span className="search-facet-label">Sources</span>
              <ChipMultiSelect options={INBOX_PROVIDER_OPTIONS} selected={providerFilter} onToggle={toggleProviderFilter} label="Inbox source filter" />
            </div>
            <div className="search-facet-group"><span className="search-facet-label">State</span>
              <ChipMultiSelect options={INBOX_STATUS_OPTIONS} selected={statusFilter} onToggle={toggleStatusFilter} label="Inbox state filter" />
            </div>
            <div className="search-facet-group"><span className="search-facet-label">Labels</span>
              <ChipMultiSelect options={INBOX_GATE_OPTIONS} selected={gateFilter} onToggle={toggleGateFilter} label="Inbox label filter" />
            </div>
            <div className="search-facet-group"><span className="search-facet-label">To</span>
              <ChipMultiSelect options={audienceOptions} selected={audienceFilter} onToggle={toggleAudienceFilter} label="Inbox audience filter" />
            </div>
            <div className="search-facet-group"><span className="search-facet-label">Owner</span>
              <ChipMultiSelect options={ownerOptions} selected={ownerFilter} onToggle={toggleOwnerFilter} label="Inbox owner filter" />
            </div>
            {projectOptions.length > 0 && <div className="search-facet-group"><span className="search-facet-label">Project</span>
              <ChipMultiSelect options={projectOptions} selected={projectFilter} onToggle={toggleProjectFilter} label="Inbox project filter" />
            </div>}
            {authorOptions.length > 0 && <div className="search-facet-group"><span className="search-facet-label">Author</span>
              <ChipMultiSelect options={authorOptions} selected={authorFilter} onToggle={toggleAuthorFilter} label="Inbox author filter" />
            </div>}
            <div className="search-facet-group"><span className="search-facet-label">Type</span>
              <ChipMultiSelect options={INBOX_KIND_OPTIONS} selected={kindFilter} onToggle={toggleKindFilter} label="Inbox type filter" />
            </div>
          </div>
          <button
            onClick={() => runAction({ action: 'sync', provider: 'github' }, 'GitHub inbox synced.')}
            disabled={STATIC || !github?.enabled}
            title={github?.enabled ? 'Pull current issues and PRs through gh.' : 'Enable github in config.yaml to sync GitHub items.'}
          >
            Sync GitHub
          </button>
        </div>
        {runningTeams.length > 0 && (
          <TeamPresence teams={runningTeams} onMessage={(recipient) => startConversation(recipient)} />
        )}
        <details ref={conversationDetails} className="local-create conversation-create">
          <summary>New conversation</summary>
          <InboxComposer
            key={`conversation-${conversationSeed}`}
            runAction={runAction}
            conversation
            recipientOptions={conversationRecipients}
            initialRecipients={conversationTarget}
          />
        </details>
        <details className="local-create">
          <summary>New inbox item</summary>
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
          {itemGroups.map((group) => (
            <section key={group.key} className="day-group inbox-day-group">
              <div className="day-heading"><h3>{group.label}</h3><span>{group.items.length}</span></div>
              {group.items.map((item) => (
                <InboxCard
                  key={`${item.provider}-${item.id}`}
                  item={item}
                  providers={providers}
                  runAction={runAction}
                />
              ))}
            </section>
          ))}
          {items.length === 0 && <p className="empty inbox-empty">No inbox items match the current filters.</p>}
        </div>
      </Panel>
    </div>
  );
}

type InboxRecipientOption = { value: string; label: string; detail?: string };
type InboxTeam = {
  run: string;
  task: string;
  taskTitle: string;
  recipient: string;
  projects: string[];
};

function activeInboxTeams(state: HorizonState): InboxTeam[] {
  const tasks = new Map((state.tasks ?? []).map((task: any) => [String(task.id), task]));
  return (state.runs ?? [])
    .filter((run: any) => run.status === 'running')
    .map((run: any) => {
      const sessions = flattenSessions(run.sessions ?? []);
      const active = [...sessions].reverse().find((session: any) => session.status === 'running')
        ?? sessions[sessions.length - 1];
      const task = String(active?.meta?.task_id || run.focus?.task || run.focus?.tasks?.[0] || '');
      const taskRecord: any = tasks.get(task);
      const projects = (active?.meta?.projects ?? run.focus?.projects ?? [])
        .map((project: unknown) => String(project))
        .filter(Boolean);
      return {
        run: String(run.id),
        task,
        taskTitle: String(taskRecord?.title || taskRecord?.objective || ''),
        recipient: task ? `task:${task}` : `run:${run.id}`,
        projects,
      };
    });
}

function TeamPresence({ teams, onMessage }: { teams: InboxTeam[]; onMessage: (recipient: string) => void }) {
  return (
    <section className="team-presence" aria-label="Running teams">
      <div className="team-presence-heading">
        <strong>Running teams</strong>
        <span>{teams.length}</span>
      </div>
      <div className="team-presence-list">
        {teams.map((team) => (
          <div className="team-presence-row" key={`${team.run}-${team.recipient}`}>
            <span className="team-live-dot" aria-hidden="true" />
            <span className="team-run">run {team.run}</span>
            {team.task && <RefChip token={team.task} />}
            <strong title={team.taskTitle}>{team.taskTitle || 'Workspace session'}</strong>
            {team.projects.length > 0 && <span className="team-projects">{team.projects.join(', ')}</span>}
            <button type="button" onClick={() => onMessage(team.recipient)}>Message</button>
          </div>
        ))}
      </div>
    </section>
  );
}

function InboxComposer({
  runAction,
  conversation = false,
  recipientOptions = [],
  initialRecipients = [],
}: {
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
  conversation?: boolean;
  recipientOptions?: InboxRecipientOption[];
  initialRecipients?: string[];
}) {
  const [kind, setKind] = useState(conversation ? 'conversation' : 'hint');
  const [author, setAuthor] = useState('human');
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [recipients, setRecipients] = useState<Set<string>>(() => new Set(initialRecipients));
  const [busy, setBusy] = useState(false);

  if (STATIC) return null;

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const cleanAuthor = author.trim();
    const cleanTitle = title.trim();
    const cleanDescription = description.trim();
    if (!cleanAuthor || !cleanTitle || !cleanDescription || busy || (conversation && recipients.size === 0)) return;
    setBusy(true);
    runAction({
      action: 'add',
      kind: conversation ? 'conversation' : kind,
      author: cleanAuthor,
      title: cleanTitle,
      comment: cleanDescription,
      ...(conversation ? {
        conversation: true,
        audience: [...recipients].join(', '),
      } : {}),
    }, conversation ? 'Conversation started.' : 'Local inbox item added.')
      .then((ok) => {
        if (!ok) return;
        setAuthor('human');
        setTitle('');
        setDescription('');
        setRecipients(new Set());
      })
      .finally(() => setBusy(false));
  };

  return (
    <form className="composer" onSubmit={submit}>
      {conversation && (
        <fieldset className="conversation-recipients">
          <legend>Participants</legend>
          <div className="conversation-recipient-options">
            {recipientOptions.map((option) => (
              <label key={option.value} className={recipients.has(option.value) ? 'selected' : ''}>
                <input
                  type="checkbox"
                  checked={recipients.has(option.value)}
                  onChange={() => setRecipients((current) => {
                    const next = new Set(current);
                    if (next.has(option.value)) next.delete(option.value);
                    else next.add(option.value);
                    return next;
                  })}
                />
                <span>{option.label}</span>
                {option.detail && <small>{option.detail}</small>}
              </label>
            ))}
          </div>
        </fieldset>
      )}
      <div className="composer-row">
        {conversation
          ? <span className="type-chip type-conversation">conversation</span>
          : <select value={kind} onChange={(event) => setKind(event.target.value)} aria-label="Inbox type">
              {INBOX_KIND_OPTIONS.filter((option) => option !== 'conversation').map((option) => <option key={option} value={option}>{option}</option>)}
            </select>}
        <input value={author} placeholder="Who is creating this item?" onChange={(event) => setAuthor(event.target.value)} />
      </div>
      <input value={title} placeholder={conversation ? 'Topic' : 'Title'} onChange={(event) => setTitle(event.target.value)} />
      <textarea value={description} placeholder={conversation ? 'Opening message' : 'Description'} onChange={(event) => setDescription(event.target.value)} rows={3} />
      <button className="primary" type="submit" disabled={!author.trim() || !title.trim() || !description.trim() || busy || (conversation && recipients.size === 0)}>{conversation ? 'Start conversation' : 'Add inbox item'}</button>
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
  const canArchive = item.provider === 'local';
  const sourceUrl = inboxSourceUrl(item, provider.repo ?? undefined);
  const comments = inboxComments(item);
  const history = historyOf(item).map((entry) => (
    entry.field === 'created'
      ? {
          ...entry,
          provenance: entry.provenance ?? item.metadata?.provenance,
          agent: entry.agent ?? item.metadata?.agent,
        }
      : entry
  ));
  const gate = inboxGate(item.labels ?? []);
  const { title, body } = inboxTitleAndBody(item);
  const itemNumber = item.metadata?.number ? `#${item.metadata.number}` : item.id;
  const author = inboxAuthor(item);
  const agent = inboxAgent(item);
  const owner = inboxOwnerTask(item);
  const readers = inboxReadBy(item);
  const scopedProjects = scopeTargets(item, 'projects');
  const recipients = inboxAudienceTargets(item);
  const participants = inboxParticipants(item);
  const isConversation = item.kind === 'conversation'
    || Boolean(item.metadata?.conversation)
    || recipients.length > 1
    || recipients.some((recipient) => recipient.startsWith('task:') || recipient.startsWith('run:'));
  const itemStatus = normalizedInboxStatus(item.status);
  const unreadForHuman = isConversation
    && participants.includes('human')
    && !readers.includes('human')
    && itemStatus === 'open';
  const activityCount = comments.length + history.length + (body ? 1 : 0);
  const [visibleKind, setVisibleKind] = useState(item.kind);
  const [visibleStatus, setVisibleStatus] = useState(itemStatus);
  const [visibleGate, setVisibleGate] = useState(gate);

  useEffect(() => {
    setVisibleKind(item.kind);
    setVisibleStatus(itemStatus);
    setVisibleGate(gate);
  }, [item.id, item.kind, itemStatus, gate]);

  const pseudoProvider = author?.toLowerCase() === 'ground' ? 'ground' : author?.toLowerCase() === 'horizon' ? 'horizon' : item.provider;

  const toggleExpanded = () => {
    const next = !expanded;
    setExpanded(next);
    if (next && unreadForHuman && caps.has('read_state')) {
      runAction({ action: 'read', provider: item.provider, id: item.id, reader: 'human' });
    }
  };

  const deleteLocal = () => {
    if (!window.confirm(`Delete local inbox item ${item.id}? This cannot be undone.`)) return;
    runAction({ action: 'delete', provider: 'local', id: item.id }, 'Local inbox item deleted.');
  };

  const isArchived = item.status === 'archived';
  const toggleArchive = () => {
    runAction(
      { action: isArchived ? 'unarchive' : 'archive', provider: item.provider, id: item.id },
      isArchived ? 'Inbox item unarchived.' : 'Inbox item archived (hidden by default).',
    );
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
    const nextAction = nextStatus === 'open'
      ? (isArchived ? 'unarchive' : 'reopen')
      : nextStatus === 'archived' ? 'archive' : 'complete';
    runAction({
      action: nextAction,
      provider: item.provider,
      id: item.id,
    }, nextStatus === 'open'
      ? 'Inbox item reopened.'
      : nextStatus === 'archived' ? 'Inbox item archived (hidden by default).' : 'Inbox item closed.'
    ).then((ok) => {
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
        <div className="issue-title-line" onClick={toggleExpanded}>
          <div className="issue-title-main">
            <SourceMark provider={pseudoProvider} />
            <div className="issue-title-stack">
              <span className="issue-title"><InlineMarkdown content={title} /></span>
              <div className="issue-quick-meta">
                <span className={`source-chip ${item.provider}`}>{item.provider}</span>
                {isConversation && item.kind !== 'conversation' && <span className="conversation-chip">conversation</span>}
                {unreadForHuman && <span className="conversation-unread-chip">unread reply</span>}
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
                    <option className="state-closed" value="closed">closed</option>
                    {canArchive && <option className="state-archived" value="archived">archived</option>}
                  </select>
                ) : <Status value={normalizedInboxStatus(item.status)} label={filterLabel(normalizedInboxStatus(item.status))} />}
                {editable && caps.has('label') ? (
                  <select className={`chip-select gate ${visibleGate}`} value={visibleGate} onClick={stop} onChange={(event) => updateGate(event.target.value)} aria-label="Agent label">
                    <option className="gate-accept" value="accept">agent-ready</option>
                    <option className="gate-pending" value="pending">not-ready</option>
                    <option className="gate-reject" value="reject">rejected</option>
                    <option className="gate-clear" value="clear">unlabeled</option>
                  </select>
                ) : <Status value={gate} label={gateLabel(gate)} />}
                {owner
                  ? <span className="tag-chip tag-ref-owner">@{owner}</span>
                  : <span className="owner-shared-chip">shared</span>}
                {isConversation && participants.length > 0 && (
                  <span className="recipient-summary" title={participants.join(' · ')}>participants {participants.join(' · ')}</span>
                )}
                {author && <span className="author-tag">by {author}{agent && <span className="agent-tag"> · {agent}</span>}</span>}
              </div>
            </div>
          </div>
          <div className="issue-title-actions">
            {activityCount > 0 && (
              <span className="comment-count" title={`${activityCount} activity ${activityCount === 1 ? 'entry' : 'entries'}`}>{activityCount}</span>
            )}
            {editable && canArchive && (
              <button className="text-action" onClick={(event) => { stop(event); toggleArchive(); }} title={isArchived ? 'Restore from archive' : 'Archive (soft-delete: kept but hidden by default)'}>
                {isArchived ? 'Unarchive' : 'Archive'}
              </button>
            )}
            {editable && item.provider === 'local' && (
              <button className="icon-danger" onClick={(event) => { stop(event); deleteLocal(); }} title="Delete local item" aria-label="Delete local item"><TrashIcon /></button>
            )}
            <span className="ev-caret">{expanded ? '▾' : '▸'}</span>
          </div>
        </div>
        {expanded && (
          <div className="issue-body">
            <div className="issue-meta-grid">
              <div><strong>ID</strong><span>{itemNumber}</span></div>
              <div><strong>Author</strong><span>{author || 'unknown'}{agent ? ` · ${agent}` : ''}</span></div>
              <div><strong>Owner</strong><span>{owner ? <RefChip token={owner} label={`@${owner}`} /> : 'shared'}</span></div>
              <div><strong>To</strong><span>{recipients.length ? recipients.join(' · ') : 'general'}</span></div>
              {isConversation && <div><strong>Participants</strong><span>{participants.join(' · ') || 'unknown'}</span></div>}
              <div><strong>Projects</strong><span>{scopedProjects.length ? scopedProjects.join(', ') : 'workspace-wide'}</span></div>
              <div><strong>Labels</strong><span>{(item.labels ?? []).join(', ') || 'none'}</span></div>
              <div><strong>Read by</strong><span>{readers.join(', ') || 'nobody'}</span></div>
              <div><strong>Opened</strong><span>{formatDate(item.created_at)}</span></div>
              {item.updated_at && item.updated_at !== item.created_at && <div><strong>Updated</strong><span>{formatDate(item.updated_at)}</span></div>}
              {item.metadata?.provenance && <div className="issue-meta-source"><strong>Source</strong><ProvenanceText provenance={item.metadata.provenance} /></div>}
              {sourceUrl && <div><strong>External</strong><span><a href={sourceUrl} target="_blank" rel="noreferrer">Open on GitHub</a></span></div>}
            </div>
            <ActivityTimeline
              comments={comments}
              history={history}
              editable={editable && item.provider === 'local'}
              itemId={item.id}
              runAction={runAction}
              description={body ? {
                body,
                author: author || (isGithub ? 'GitHub description' : 'Description'),
                agent,
                at: item.created_at,
                editable: editable && item.provider === 'local',
                provenance: item.metadata?.provenance,
                title,
              } : undefined}
            />
            {editable && caps.has('comment') && <CommentBox item={item} runAction={runAction} />}
          </div>
        )}
      </div>
    </article>
  );
}

// Legacy agent messages sometimes arrived as one very long paragraph. Preserve
// stored Markdown, but add display-only paragraph boundaries when the message is
// both long and entirely unstructured. New writes are validated by the CLI.
function readableMessageMarkdown(content: string): string {
  const text = content.trim();
  if (text.length < 600) return content;
  if (/\n\s*\n|(?:^|\n)\s*(?:#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s*|```|\|)/m.test(text)) {
    return content;
  }

  const sectionLead = /\s+(?=(?:(?:WHY|WHAT|WHEN|WHERE|HOW|FIRST|SECOND|THIRD|FOURTH|CORRECTION|RETRACTION|CONCLUSION|FINAL STATE|PRACTICAL CONSEQUENCE|THE PART|THE ONE|ITEM \d+|NOTHING IN|Separately)\b[^:\n]{0,120}:))/g;
  const withSections = text.replace(sectionLead, '\n\n');
  return withSections
    .split(/\n\s*\n/)
    .flatMap((paragraph) => structureDenseParagraph(paragraph))
    .join('\n\n');
}

function structureDenseParagraph(paragraph: string): string[] {
  const declaration = /\b(?:thm|lem|def|inst|prop|cor):[A-Za-z0-9_.-]+/g;
  const declarations = [...paragraph.matchAll(declaration)];
  if (declarations.length >= 2 && declarations[0].index !== undefined) {
    const first = declarations[0].index;
    const prefix = paragraph.slice(0, first).trim();
    const list = paragraph
      .slice(first)
      .replace(/\s+(?=(?:thm|lem|def|inst|prop|cor):[A-Za-z0-9_.-]+)/g, '\n- ');
    return [prefix, `- ${list}`].filter(Boolean);
  }
  if (paragraph.length <= 520) return [paragraph];

  const clauses = paragraph.split(/(?<=[.!?;])\s+(?=[A-Z0-9([])/);
  const chunks: string[] = [];
  let current = '';
  for (const clause of clauses) {
    if (current && current.length + clause.length > 520) {
      chunks.push(current);
      current = clause;
    } else {
      current = current ? `${current} ${clause}` : clause;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}

function EditableDescription({
  body,
  createdAt,
  editable,
  itemId,
  runAction,
  title,
  titleLabel,
  agent,
  provenance,
}: {
  body: string;
  createdAt: string;
  editable: boolean;
  itemId: string;
  runAction: (payload: Record<string, unknown>, success?: string) => Promise<boolean>;
  title: string;
  titleLabel: string;
  agent?: string;
  provenance?: any;
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
        {agent && <span className="agent-tag">{agent}</span>}
        <ActivityProvenance author={titleLabel} provenance={provenance} />
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
        <div className="comment-body"><MarkdownBlock content={readableMessageMarkdown(body)} /></div>
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
        {comment.agent && <span className="agent-tag">{comment.agent}</span>}
        {comment._description && <span className="comment-tag">description</span>}
        <ActivityProvenance author={author} provenance={comment.provenance} />
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
        <div className="comment-body"><MarkdownBlock content={readableMessageMarkdown(body)} /></div>
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
  const [author, setAuthor] = useState(item.provider === 'local' ? 'human' : '');
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
      setAuthor(item.provider === 'local' ? 'human' : '');
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
  const from = e.from ? historyValueLabel(e.from) : '—';
  const to = e.to ? historyValueLabel(e.to) : '—';
  switch (e.field) {
    case 'created': return 'opened this item';
    case 'deleted': return 'deleted this item';
    case 'status': return `status ${from} → ${to}`;
    case 'label': return `labels ${from} → ${e.to || '—'}`;
    case 'kind': return `type ${from} → ${e.to || '—'}`;
    case 'read_by': return 'marked this item read';
    case 'owner': return `owner ${from} → ${to}`;
    case 'audience': return `audience ${from} → ${to}`;
    case 'body': return e.note || 'edited the description';
    case 'edited': return e.note || 'edited fields';
    default: return e.note || e.field || 'changed';
  }
}

function historyValueLabel(value: unknown) {
  return filterLabel(String(value || ''));
}

function HistoryRow({ entry }: { entry: any }) {
  return (
    <div className="history-row" title={formatDate(entry.at)}>
      <span className="history-dot" aria-hidden="true" />
      <span className="history-text"><strong>{entry.actor || 'system'}</strong> {describeHistory(entry)}</span>
      {entry.agent && <span className="agent-tag">{entry.agent}</span>}
      <ActivityProvenance author={entry.actor} provenance={entry.provenance} />
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
  description?: { body: string; author?: string; agent?: string; at?: string; editable?: boolean; provenance?: any; title?: string };
}) {
  const ACTIVITY_PAGE = 5;
  const [visibleCount, setVisibleCount] = useState(ACTIVITY_PAGE);
  const commentTime = (c: any) => String(c.at ?? c.created_at ?? c.createdAt ?? '');
  useEffect(() => {
    setVisibleCount(ACTIVITY_PAGE);
  }, [itemId, comments.length, history.length, description?.body]);
  const entries: any[] = [
    ...(description?.body ? [{
      kind: 'description',
      at: String(description.at ?? ''),
      rank: 1,
      description,
      index: 0,
    }] : []),
    ...comments.map((comment, index) => ({
      kind: 'comment',
      at: commentTime(comment),
      rank: 2,
      comment,
      index,
    })),
    ...history.map((entry, index) => ({
      kind: 'event',
      // "opened" logically precedes the initial description even though its
      // append-only history write may be a few milliseconds later.
      at: entry.field === 'created' && description?.at
        ? String(description.at)
        : String(entry.at ?? ''),
      rank: entry.field === 'created' ? 0 : 3,
      entry,
      index,
    })),
  ].sort((a, b) => {
    const aTime = Date.parse(a.at) || 0;
    const bTime = Date.parse(b.at) || 0;
    return aTime - bTime || a.rank - b.rank || (a.index ?? 0) - (b.index ?? 0);
  });
  if (!entries.length) return null;
  const hidden = Math.max(0, entries.length - visibleCount);
  const visibleEntries = entries.slice(hidden);
  return (
    <div className="issue-details">
      <div className="comment-thread">
        <div className="comment-thread-title">{title}</div>
        {hidden > 0 && (
          <button
            className="timeline-load-more"
            type="button"
            onClick={() => setVisibleCount((count) => Math.min(entries.length, count + ACTIVITY_PAGE))}
          >
            Load {Math.min(hidden, ACTIVITY_PAGE)} earlier activities ({hidden} remaining)
          </button>
        )}
        {visibleEntries.map((entry) => entry.kind === 'description' ? (
          entry.description.editable ? (
            <EditableDescription
              body={entry.description.body}
              createdAt={entry.description.at ?? ''}
              editable
              itemId={itemId}
              key="description"
              runAction={runAction}
              title={entry.description.title ?? itemId}
              titleLabel={entry.description.author || 'Description'}
              agent={entry.description.agent}
              provenance={entry.description.provenance}
            />
          ) : (
            <EditableComment
              comment={{
                author: entry.description.author,
                agent: entry.description.agent,
                at: entry.description.at,
                body: entry.description.body,
                provenance: entry.description.provenance,
                _description: true,
              }}
              editable={false}
              index={-1}
              itemId={itemId}
              key="description"
              runAction={runAction}
            />
          )
        ) : entry.kind === 'comment' ? (
          <EditableComment
            comment={entry.comment}
            editable={editable}
            index={entry.index}
            itemId={itemId}
            key={`c-${entry.comment.id ?? entry.index}`}
            runAction={runAction}
          />
        ) : (
          <HistoryRow entry={entry.entry} key={`h-${entry.index}`} />
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
  const parts = provenanceParts(provenance);
  if (!parts.length) return null;
  return <span className="provenance-chip" title="Authoring role · run · session · task">{parts.join(' · ')}</span>;
}

function ActivityProvenance({ author, provenance }: { author?: string; provenance: any }) {
  if (provenanceParts(provenance).length > 0) {
    return <ProvenanceChip provenance={provenance} />;
  }
  if (String(author || '').toLowerCase() === 'horizon') {
    return <span className="provenance-missing" title="This legacy activity predates per-action provenance.">session not recorded</span>;
  }
  return null;
}

function ProvenanceText({ provenance }: { provenance: any }) {
  const parts = provenanceParts(provenance);
  if (!parts.length) return null;
  return <span className="provenance-text">{parts.join(' · ')}</span>;
}

function provenanceParts(provenance: any): string[] {
  if (!provenance || typeof provenance !== 'object') return [];
  const parts: string[] = [];
  if (provenance.role) parts.push(String(provenance.role));
  if (provenance.run) parts.push(`run ${provenance.run}`);
  if (provenance.session) parts.push(String(provenance.session));
  if (provenance.subagent) parts.push(String(provenance.subagent));
  if (provenance.task) parts.push(`task ${provenance.task}`);
  return parts;
}

function Transcripts({ state }: { state?: any }) {
  const TRANSCRIPT_PAGE_SIZE = 120;
  const runs = state?.runs ?? [];
  // Newest run first, so an in-progress run is at the top of the sidebar.
  const orderedRuns = [...runs].sort(compareRuns);
  const [events, setEvents] = useState<any[] | null>(null);
  const [report, setReport] = useState<string>('');
  const [recommendation, setRecommendation] = useState<string>('');
  const [before, setBefore] = useState<number | null>(null);
  const [hasOlder, setHasOlder] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [transcriptError, setTranscriptError] = useState('');
  const [selected, setSelected] = useState<string>('');
  const [now, setNow] = useState(() => Date.now());
  const selectedSession = useMemo(() => findSessionByRef(runs, selected), [runs, selected]);
  const selectedRun = useMemo(() => findRunBySessionRef(runs, selected), [runs, selected]);
  const selectedNextStart = useMemo(
    () => selectedRun && selectedSession ? nextSessionStart(selectedRun.sessions ?? [], selectedSession) : undefined,
    [selectedRun, selectedSession],
  );
  const activeTickSession = useMemo(() => activeRunSessionKey(selectedRun), [selectedRun]);
  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Deep-link / selection: keep `selected` in sync with the URL.
  useEffect(() => {
    setSelected(searchParams.get('ref') ?? '');
  }, [searchParams]);

  // Live-tail the open transcript + its report: fetch on select, then poll so
  // new events and the final report appear without a manual page refresh.
  //
  // Only a *live* session needs the tail. A `session_end` in the stream means the
  // transcript is closed and can never gain another event, so we stop polling it
  // — re-fetching a finished multi-MB transcript every 3s rebuilt every event
  // object, which defeated the row memoization and re-ran the markdown/KaTeX
  // conversion in every expanded panel. `report.md` is written just *after* the
  // stream closes, so keep polling a couple of cycles past `session_end` to catch
  // it before going quiet for good.
  useEffect(() => {
    if (!selected) {
      setEvents(null);
      setReport('');
      setRecommendation('');
      setBefore(null);
      setHasOlder(false);
      setTranscriptError('');
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let graceLeft = 2;
    let initialized = false;
    setEvents(null);
    setBefore(null);
    setHasOlder(false);
    setTranscriptError('');
    const load = async () => {
      const reportRequest = getReport(selected).then((value) => value, () => null);
      const tx = await getTranscriptPage(selected, undefined, TRANSCRIPT_PAGE_SIZE)
        .then((value) => value, () => null);
      if (cancelled) return;
      if (tx) {
        setEvents((current) => initialized
          ? mergeTranscriptPages(current ?? [], tx.events ?? [])
          : (tx.events ?? []));
        if (!initialized) {
          setBefore(tx.before ?? null);
          setHasOlder(Boolean(tx.has_more));
        }
        setTranscriptError('');
      } else if (!initialized) {
        setEvents([]);
        setTranscriptError('Unable to load this session.');
      }
      initialized = true;
      void reportRequest.then((rep) => {
        if (cancelled) return;
        setReport(rep?.markdown ?? '');
        setRecommendation(rep?.recommendation ?? '');
      });
      const ended = Boolean(tx?.events?.some((event: any) => event?.kind === 'session_end'));
      if (ended && graceLeft-- <= 0) return;
      timer = setTimeout(load, 3000);
    };
    load();
    return () => { cancelled = true; if (timer !== undefined) clearTimeout(timer); };
  }, [selected]);

  const loadOlder = () => {
    if (!selected || before == null || loadingOlder) return;
    setLoadingOlder(true);
    setTranscriptError('');
    getTranscriptPage(selected, before, TRANSCRIPT_PAGE_SIZE)
      .then((page) => {
        setEvents((current) => mergeTranscriptPages(page.events ?? [], current ?? []));
        setBefore(page.before ?? null);
        setHasOlder(Boolean(page.has_more));
      })
      .catch(() => setTranscriptError('Unable to load older entries.'))
      .finally(() => setLoadingOlder(false));
  };

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
            }} now={now} />
          ))}
        </div>
      </aside>
      <TranscriptViewer
        events={events}
        harnesses={state?.harnesses ?? {}}
        report={report}
        recommendation={recommendation}
        run={selectedRun}
        selected={selected}
        session={selectedSession}
        runId={selectedRun?.id}
        nextSessionStart={selectedNextStart}
        activeTickSession={activeTickSession}
        hasOlder={hasOlder}
        loadingOlder={loadingOlder}
        onLoadOlder={loadOlder}
        transcriptError={transcriptError}
      />
    </div>
  );
}

function mergeTranscriptPages(left: any[], right: any[]): any[] {
  const byCursor = new Map<number | string, any>();
  let fallback = 0;
  for (const event of [...left, ...right]) {
    const key = event?._cursor ?? `fallback-${fallback++}-${event?.at ?? ''}-${event?.kind ?? ''}`;
    byCursor.set(key, event);
  }
  return [...byCursor.values()].sort((a, b) => {
    if (typeof a?._cursor === 'number' && typeof b?._cursor === 'number') {
      return a._cursor - b._cursor;
    }
    return (Date.parse(a?.at ?? '') || 0) - (Date.parse(b?.at ?? '') || 0);
  });
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

/** How many log entries a list renders before it asks. Also the size of one
 * "show more" step. Sessions run to ~900 events at the tail, and a single tool
 * result can be hundreds of KB, so rendering a whole session on open costs far
 * more than anyone reads — the newest events are the ones you came for. */
const EVENT_PAGE = 120;

/** One subagent's events as a collapsible sublog.
 *
 * The parent list counts a whole group as one entry, so a subagent that ran
 * hundreds of events would paint all of them in a single frame no matter how the
 * outer cap is set. Cap the inner list the same way. (Collapsing wouldn't help:
 * `<details>` mounts its children whether or not it's open.) */
function SubagentSublog({ group }: { group: Extract<LogGroup, { sub: true }> }) {
  const [limit, setLimit] = useState(EVENT_PAGE);
  const model = group.items.find(({ event }) => event?.data?.model)?.event?.data?.model;
  const hidden = Math.max(0, group.items.length - limit);
  return (
    <details className="log-panel subagent-sublog" open>
      <summary>
        <span className="role-badge role-subagent">S</span>
        <span className="subagent-name">{group.label}</span>
        {model ? <span className="subagent-model" title="Model used by this subagent">{model}</span> : null}
        <UsageChips usage={subagentGroupUsage(group.items)} />
        <span className="subagent-count">{group.items.length} events</span>
      </summary>
      <div className="log-lines sublog-lines">
        {group.items.slice(0, limit).map(({ event, idx }) => (
          <TranscriptEvent key={idx} event={event} forceOpen={null} />
        ))}
        {hidden > 0 && <ShowMoreButton hidden={hidden} onMore={() => setLimit((l) => l + EVENT_PAGE)} />}
      </div>
    </details>
  );
}

function ShowMoreButton({ hidden, onMore }: { hidden: number; onMore: () => void }) {
  return (
    <button type="button" className="log-expand-more" onClick={onMore}>
      Show {Math.min(hidden, EVENT_PAGE)} more
      <span className="log-expand-rest">{hidden} older {hidden === 1 ? 'entry' : 'entries'} hidden</span>
    </button>
  );
}

function sumUsage(usages: any[]) {
  let hasCost = false;
  const total = usages.reduce((acc, usage) => {
    if (!usage) return acc;
    const values = usageNumbers(usage);
    acc.tokens_in += values.tokensIn;
    acc.tokens_out += values.tokensOut;
    acc.cached_tokens_in += values.cachedIn;
    acc.reasoning_tokens_out += values.reasoningOut;
    if (values.cost !== null) {
      acc.cost_usd += values.cost;
      hasCost = true;
    }
    return acc;
  }, {
    tokens_in: 0,
    tokens_out: 0,
    cached_tokens_in: 0,
    reasoning_tokens_out: 0,
    cost_usd: 0,
  });
  return {
    ...total,
    cost_usd: hasCost ? total.cost_usd : null,
  };
}

function subagentGroupUsage(items: { event: any; idx: number }[]) {
  return sumUsage(items.map(({ event }) => event.usage ?? (event.kind === 'usage' ? event.data : null)));
}

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

// A signed count with a +/− sign, coloured green when it means progress.
function Delta({ value, goodWhenNegative = false }: { value: number; goodWhenNegative?: boolean }) {
  const good = goodWhenNegative ? value < 0 : value > 0;
  const cls = value === 0 ? 'zero' : good ? 'good' : 'bad';
  return <span className={`delta ${cls}`}>{value > 0 ? `+${value}` : value === 0 ? '±0' : `−${Math.abs(value)}`}</span>;
}

// An "after (Δ)" stat, e.g. 6 (+1) — the resulting value with its signed change.
function AfterDelta({ after, delta, goodWhenNegative = false }: { after: number; delta: number; goodWhenNegative?: boolean }) {
  return (
    <span className="after-delta">
      <strong>{after}</strong>
      {delta !== 0 && <> (<Delta value={delta} goodWhenNegative={goodWhenNegative} />)</>}
    </span>
  );
}

const DECL_KIND_LABELS: Record<string, string> = {
  theorem: 'thm',
  proposition: 'prop',
  corollary: 'cor',
  definition: 'def',
  lemma: 'lemma',
  def: 'def',
  abbrev: 'abbrev',
  instance: 'inst',
  structure: 'struct',
  inductive: 'ind',
  class: 'class',
  example: 'ex',
  conjecture: 'conj',
  remark: 'rem',
  notation: 'notn',
  convention: 'conv',
};

const DECL_KIND_ORDER = [
  'theorem', 'lemma', 'proposition', 'corollary', 'definition', 'def',
  'abbrev', 'instance', 'structure', 'inductive', 'class', 'example',
  'conjecture', 'remark', 'notation', 'convention',
];

function declEntries(delta?: Record<string, number>) {
  if (!delta) return [];
  return Object.entries(delta)
    .filter(([, value]) => value !== 0)
    .sort(([a], [b]) => {
      const ai = DECL_KIND_ORDER.indexOf(a);
      const bi = DECL_KIND_ORDER.indexOf(b);
      if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
      return a.localeCompare(b);
    });
}

function DeclarationDelta({ delta, compact = false }: { delta?: Record<string, number>; compact?: boolean }) {
  const entries = declEntries(delta);
  if (!entries.length) return null;
  return (
    <span className={`decl-delta ${compact ? 'compact' : ''}`} title="Declaration/environment count changes">
      {entries.map(([kind, value]) => (
        <span key={kind} className="decl-delta-item">
          <Delta value={value} /> {DECL_KIND_LABELS[kind] ?? kind}
        </span>
      ))}
    </span>
  );
}

function GitUnifiedDiff({ text }: { text: string }) {
  const lines = text.split('\n');
  return (
    <pre className="change-diff"><code>{lines.map((line, i) => {
      const cls = line.startsWith('+') && !line.startsWith('+++') ? 'add'
        : line.startsWith('-') && !line.startsWith('---') ? 'del'
        : line.startsWith('@@') ? 'hunk'
        : line.startsWith('diff ') || line.startsWith('index ') || line.startsWith('+++') || line.startsWith('---') ? 'meta'
        : '';
      return <div key={i} className={`diff-line ${cls}`}>{line || ' '}</div>;
    })}</code></pre>
  );
}

// One file row: click the name to lazily load and expand its diff. Only the
// base file name is shown (paths are often very long); hover reveals the path.
function FileChangeRow({ runId, session, file, showComments, sha }: {
  runId: string; session: string; file: SessionChangeFile; showComments: boolean; sha: string;
}) {
  const [open, setOpen] = useState(false);
  const [diff, setDiff] = useState<FileDiff | null>(null);
  const before = showComments ? file.loc_before ?? 0 : file.loc_code_before ?? 0;
  const after = showComments ? file.loc_after ?? 0 : file.loc_code_after ?? 0;
  const name = file.path.split('/').pop() || file.path;
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && diff === null) {
      // Commit-granular: exactly what this one commit changed in the file.
      getSessionFileDiff(runId, session, file.path, sha).then(setDiff).catch(() => setDiff({ path: file.path, available: false, diff: '' }));
    }
  };
  return (
    <>
      <tr className={open ? 'change-row open' : 'change-row'}>
        <td className="change-file">
          <button className="change-file-btn" onClick={toggle} title={file.path}>
            <span className="change-caret">{open ? '▾' : '▸'}</span>
            <span className="change-fname">{name}</span>
            {file.added && <span className="file-tag added">new</span>}
            {file.deleted && <span className="file-tag deleted">del</span>}
            <DeclarationDelta delta={file.decl_delta} compact />
          </button>
        </td>
        {file.category === 'lean' && (
          <td className="change-sorry">
            <AfterDelta after={file.sorry_after ?? 0} delta={file.sorry_delta ?? 0} goodWhenNegative /> sorry
          </td>
        )}
        <td className="change-loc"><AfterDelta after={after} delta={after - before} /> {showComments ? 'loc' : 'code'}</td>
        <td className="change-churn muted" title="raw line churn (git)"><span className="delta good">+{file.add ?? 0}</span>/<span className="delta bad">−{file.del ?? 0}</span></td>
      </tr>
      {open && (
        <tr className="change-diff-row">
          <td colSpan={file.category === 'lean' ? 4 : 3}>
            {diff === null ? <p className="empty">Loading diff…</p>
              : !diff.available ? <p className="empty">Diff unavailable (no VCS history for this session).</p>
              : <GitUnifiedDiff text={diff.diff} />}
            {diff?.truncated && <p className="empty">Diff truncated (large file).</p>}
          </td>
        </tr>
      )}
    </>
  );
}

// Deterministic per-session change view: agent sessions show only files from
// agent-authored semantic commits; system/integration commits remain visible as
// ledger metadata. Lean and blueprint files get their own tabs.
// One commit rendered as a distinct card: message (the progress statement) + its
// own per-file diff. Expands to that single commit's files (diff scoped by sha).
function CommitCard({ commit, runId, session }: { commit: CommitChange; runId: string; session: string }) {
  const [open, setOpen] = useState(false);
  const files = (commit.files ?? []).filter((f) => f.category === 'lean' || f.category === 'blueprint');
  const isAgent = commit.kind === 'agent';
  return (
    <div className={`commit-card ${isAgent ? 'agent' : 'system'}`}>
      <button className="commit-card-head" onClick={() => setOpen(!open)} title={commit.sha}>
        <span className="change-caret">{open ? '▾' : '▸'}</span>
        <span className={`commit-kind ${commit.kind || 'other'}`}>{isAgent ? (commit.role || 'agent') : (commit.kind || 'commit')}</span>
        <span className="commit-copy">
          <span className="commit-subject">{commit.subject}</span>
          {commit.created_at && (
            <time className="commit-time" dateTime={commit.created_at} title={commit.created_at}>
              {formatDateTime(commit.created_at)}
            </time>
          )}
        </span>
        {commit.sorry_delta !== 0 && (
          <span className="commit-stat"><AfterDelta after={commit.lean?.sorry_after ?? 0} delta={commit.sorry_delta} goodWhenNegative /> sorry</span>
        )}
        <span className="commit-sha">{commit.short_sha}</span>
      </button>
      {commit.summary?.trim() && (
        <div className="commit-summary">
          <MarkdownBlock content={commit.summary} />
        </div>
      )}
      {open && (
        files.length === 0 ? (
          <p className="empty commit-empty">No Lean/blueprint files in this commit{commit.other_count ? ` (+${commit.other_count} shared-state file${commit.other_count === 1 ? '' : 's'})` : ''}.</p>
        ) : (
          <table className="change-table">
            <tbody>
              {files.map((f) => (
                <FileChangeRow key={f.path} runId={runId} session={session} file={f} showComments sha={commit.sha} />
              ))}
            </tbody>
          </table>
        )
      )}
    </div>
  );
}

// The commit-granular "progress" panel for a session: each commit is a card whose
// message + diff IS the progress record. Fetched lazily from /api/session/commits.
function SessionCommitsPanel({ runId, session }: { runId: string; session: string }) {
  const COMMIT_PAGE = 3;
  // The inbox-activity list arrives whole in the page payload (not server-paged
  // like commits), so long sessions dumped dozens of rows at once. Cap it and
  // reveal the rest on demand, matching the Commits panel's "show more".
  const INBOX_PAGE = 6;
  const [commits, setCommits] = useState<CommitChange[] | null>(null);
  const [inbox, setInbox] = useState<SessionInboxActivity | null>(null);
  const [inboxExpanded, setInboxExpanded] = useState(false);
  const [roadmap, setRoadmap] = useState<SessionRoadmapActivity | null>(null);
  const [commitCounts, setCommitCounts] = useState<Record<string, number>>({});
  const [attempts, setAttempts] = useState<any[]>([]);
  const [checks, setChecks] = useState<any[]>([]);
  const [roadmapExpanded, setRoadmapExpanded] = useState(false);
  const [total, setTotal] = useState<number | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string>('');
  useEffect(() => {
    let live = true;
    setCommits(null);
    setInbox(null);
    setInboxExpanded(false);
    setRoadmap(null);
    setCommitCounts({});
    setAttempts([]);
    setChecks([]);
    setRoadmapExpanded(false);
    setTotal(null);
    setNextOffset(null);
    setLoadingMore(false);
    setError('');
    getSessionCommits(runId, session, 0, COMMIT_PAGE)
      .then((page) => {
        if (!live) return;
        setTotal(page.total ?? page.commits?.length ?? 0);
        setCommits(page.commits ?? []);
        setInbox(page.inbox ?? { items: [], created: 0, comments: 0, actions: 0, total: 0 });
        setRoadmap(page.roadmap ?? { items: [], created: 0, status_changes: 0, comments: 0, total: 0 });
        setCommitCounts(page.commit_counts ?? {});
        setAttempts(page.attempts ?? []);
        setChecks(page.checks ?? []);
        setNextOffset(page.has_more ? (page.next_offset ?? null) : null);
      })
      .catch(() => {
        if (!live) return;
        setCommits([]);
        setInbox({ items: [], created: 0, comments: 0, actions: 0, total: 0 });
        setRoadmap({ items: [], created: 0, status_changes: 0, comments: 0, total: 0 });
        setCommitCounts({});
        setAttempts([]);
        setChecks([]);
        setError('Unable to load commits.');
      });
    return () => { live = false; };
  }, [runId, session]);

  const loadMore = () => {
    if (nextOffset == null || loadingMore) return;
    setLoadingMore(true);
    setError('');
    getSessionCommits(runId, session, nextOffset, COMMIT_PAGE)
      .then((page) => {
        setCommits((current) => [...(current ?? []), ...(page.commits ?? [])]);
        setTotal(page.total ?? total);
        setNextOffset(page.has_more ? (page.next_offset ?? null) : null);
      })
      .catch(() => setError('Unable to load older commits.'))
      .finally(() => setLoadingMore(false));
  };
  if (commits === null) {
    return (
      <details className="log-panel commits-panel" open>
        <summary>Commits</summary>
        <p className="empty commit-loading">Loading commit history…</p>
      </details>
    );
  }
  const inboxPanel = inbox && inbox.items.length > 0 ? (
    <details className="log-panel session-inbox-panel" open>
      <summary>
        Inbox activity <span className="commits-count">{inbox.total}</span>
        <span className="session-inbox-totals">
          {inbox.created} opened · {inbox.comments} comment{inbox.comments === 1 ? '' : 's'} · {inbox.actions} action{inbox.actions === 1 ? '' : 's'}
        </span>
      </summary>
      <div className="session-inbox-list">
        {(inboxExpanded ? inbox.items : inbox.items.slice(0, INBOX_PAGE)).map((item) => (
          <Link className="session-inbox-row" key={item.id} to={`/inbox?item=${encodeURIComponent(item.id)}`}>
            <span className={`type-chip type-${item.kind}`}>{item.kind}</span>
            <span className="session-inbox-copy">
              <strong>{item.title}</strong>
              <span>{item.id} · {item.status}</span>
            </span>
            <span className="session-inbox-actions">
              {item.created && <span>opened</span>}
              {item.comments > 0 && <span>{item.comments} comment{item.comments === 1 ? '' : 's'}</span>}
              {item.actions > 0 && <span>{item.actions} action{item.actions === 1 ? '' : 's'}</span>}
            </span>
          </Link>
        ))}
        {inbox.items.length > INBOX_PAGE && (
          <button
            type="button"
            className="commit-load-more session-inbox-more"
            onClick={() => setInboxExpanded((v) => !v)}
          >
            {inboxExpanded ? 'Show fewer' : `Show ${inbox.items.length - INBOX_PAGE} more`}
            {!inboxExpanded && <span>{inbox.items.length - INBOX_PAGE} more items hidden</span>}
          </button>
        )}
      </div>
    </details>
  ) : null;
  const roadmapPanel = roadmap && roadmap.items.length > 0 ? (
    <details className="log-panel session-inbox-panel session-roadmap-panel" open>
      <summary>
        Roadmap activity <span className="commits-count">{roadmap.total}</span>
        <span className="session-inbox-totals">
          {roadmap.created} created · {roadmap.status_changes} status change{roadmap.status_changes === 1 ? '' : 's'} · {roadmap.comments} comment{roadmap.comments === 1 ? '' : 's'}
        </span>
      </summary>
      <div className="session-inbox-list">
        {(roadmapExpanded ? roadmap.items : roadmap.items.slice(0, INBOX_PAGE)).map((item) => (
          <Link className="session-inbox-row" key={item.id} to={`/roadmap?item=${encodeURIComponent(item.id)}`}>
            <span className="type-chip type-roadmap">roadmap</span>
            <span className="session-inbox-copy">
              <strong>{item.title}</strong>
              <span>{item.id} · {item.status}</span>
            </span>
            <span className="session-inbox-actions">
              {item.created && <span>created</span>}
              {item.status_changes > 0 && <span>→ {item.status_to || item.status}</span>}
              {item.comments > 0 && <span>{item.comments} comment{item.comments === 1 ? '' : 's'}</span>}
            </span>
          </Link>
        ))}
        {roadmap.items.length > INBOX_PAGE && (
          <button
            type="button"
            className="commit-load-more session-inbox-more"
            onClick={() => setRoadmapExpanded((v) => !v)}
          >
            {roadmapExpanded ? 'Show fewer' : `Show ${roadmap.items.length - INBOX_PAGE} more`}
            {!roadmapExpanded && <span>{roadmap.items.length - INBOX_PAGE} more items hidden</span>}
          </button>
        )}
      </div>
    </details>
  ) : null;
  const commitKindSummary = Object.entries(commitCounts)
    .filter(([, count]) => count > 0)
    .map(([kind, count]) => `${count} ${kind}`)
    .join(' · ');
  const attemptsPanel = attempts.length > 0 ? (
    <details className="log-panel attempts-panel">
      <summary>Rejected attempts <span className="commits-count">{attempts.length}</span></summary>
      <div className="attempt-list">
        {attempts.map((attempt) => (
          <article className="attempt-row" key={attempt.id}>
            <div><strong>{attempt.reason || 'Rejected approach'}</strong><span>{attempt.created_at ? formatDateTime(attempt.created_at) : attempt.id}</span></div>
            <span>{(attempt.files ?? []).length} file{(attempt.files ?? []).length === 1 ? '' : 's'}</span>
            <ul>{(attempt.files ?? []).map((file: any) => <li key={file.path}>{file.path}{file.lines ? ` · ${file.lines} lines` : ''}</li>)}</ul>
          </article>
        ))}
      </div>
    </details>
  ) : null;
  const checksPanel = checks.length > 0 ? (
    <details className="log-panel checks-panel">
      <summary>Lean checks <span className="commits-count">{checks.length}</span></summary>
      <div className="check-list">
        {checks.map((check, index) => (
          <div className={`check-row check-${check.ok ? 'passed' : 'failed'}`} key={`${check.finished_at || index}-${index}`}>
            <strong>{check.ok ? 'passed' : check.status || 'failed'}</strong>
            <code>{Array.isArray(check.command) ? check.command.join(' ') : 'Lean check'}</code>
            {check.duration_seconds != null && <span>{formatSeconds(Number(check.duration_seconds))}</span>}
            {check.reused && <span>reused</span>}
          </div>
        ))}
      </div>
    </details>
  ) : null;
  if (commits.length === 0) {
    if (!error) return <>{checksPanel}{attemptsPanel}{inboxPanel}{roadmapPanel}</>;
    return <>{checksPanel}{attemptsPanel}{inboxPanel}{roadmapPanel}<details className="log-panel commits-panel" open>
      <summary>Commits</summary>
      <p className="empty commit-loading">{error}</p>
    </details></>;
  }
  return (
    <>{checksPanel}{attemptsPanel}{inboxPanel}{roadmapPanel}<details className="log-panel commits-panel" open>
      <summary>
        Commits <span className="commits-count">{total == null ? commits.length : `${commits.length}/${total}`}</span>
        {commitKindSummary && <span className="session-inbox-totals">{commitKindSummary}</span>}
      </summary>
      <div className="commit-cards">
        {commits.map((c) => <CommitCard key={c.sha} commit={c} runId={runId} session={session} />)}
        {nextOffset != null && (
          <button type="button" className="commit-load-more" onClick={loadMore} disabled={loadingMore}>
            {loadingMore ? 'Loading...' : `Show ${Math.min(COMMIT_PAGE, Math.max(0, (total ?? commits.length) - commits.length))} more`}
            <span>{Math.max(0, (total ?? commits.length) - commits.length)} older commits hidden</span>
          </button>
        )}
      </div>
      {error && <p className="empty commit-loading">{error}</p>}
    </details></>
  );
}

function lifecycleIdentity(event: any): string {
  const data = event?.data ?? {};
  if (data.subagent_key) return `key:${String(data.subagent_key)}`;
  if (data.name) return `name:${String(data.name)}`;
  return '';
}

function collapseWorkflowProgress(events: any[] | null): any[] | null {
  if (!events) return events;
  const seen = new Set<string>();
  const kept: any[] = [];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.kind !== 'workflow_progress') {
      kept.push(event);
      continue;
    }
    const data = event.data ?? {};
    const identity = data.task_id || data.workflow_id || data.run_id || data.name;
    if (!identity) {
      kept.push(event);
      continue;
    }
    const key = String(identity);
    if (!seen.has(key)) {
      seen.add(key);
      kept.push(event);
    }
  }
  return kept.reverse();
}

/** Keep context accounting append-only on disk, but show only the newest
 * snapshot in the transcript. Codex emits one after nearly every model step,
 * so rendering all of them obscures the conversation on long runs. */
function collapseContextSnapshots(events: any[] | null): any[] | null {
  if (!events) return events;
  let latest = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index]?.kind === 'context') {
      latest = index;
      break;
    }
  }
  if (latest < 0) return events;
  return events.filter((event, index) => event?.kind !== 'context' || index === latest);
}

/** Pair lifecycle end rows with their dispatch row without changing the stored
 * append-only transcript. Native engines do not always report a duration, but
 * both give us timestamps and a stable tool/task identity. */
function annotateSubagentDurations(events: any[] | null): any[] | null {
  if (!events) return events;
  const starts = new Map<string, any>();
  const startsByName = new Map<string, any>();
  return events.map((event) => {
    if (event?.kind === 'subagent_start') {
      const identity = lifecycleIdentity(event);
      if (identity) starts.set(identity, event);
      if (event.data?.name) startsByName.set(String(event.data.name), event);
      return event;
    }
    if (event?.kind !== 'subagent_end' || event.data?.duration_seconds != null) return event;
    const identity = lifecycleIdentity(event);
    const start = (identity ? starts.get(identity) : null)
      ?? (event.data?.name ? startsByName.get(String(event.data.name)) : null);
    const duration = durationSeconds(start?.at, event.at);
    if (duration === null) return event;
    return {
      ...event,
      data: { ...event.data, started_at: start.at, duration_seconds: duration },
    };
  });
}

function TranscriptViewer({
  events,
  harnesses,
  report,
  recommendation,
  run,
  selected,
  session,
  runId,
  nextSessionStart,
  activeTickSession,
  hasOlder = false,
  loadingOlder = false,
  onLoadOlder,
  transcriptError = '',
}: {
  events: any[] | null;
  harnesses?: Record<string, any>;
  report?: string;
  recommendation?: string;
  run?: any;
  selected: string;
  session?: any;
  runId?: string;
  nextSessionStart?: string;
  activeTickSession?: string;
  hasOlder?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  transcriptError?: string;
}) {
  const displayEvents = useMemo(
    () => annotateSubagentDurations(collapseWorkflowProgress(collapseContextSnapshots(events))),
    [events],
  );
  const start = displayEvents?.find((event: any) => event.kind === 'session_start');
  const prompt = start?.data?.prompt ? stripAnsi(String(start.data.prompt)).trim() : '';
  const harness = start?.data?.harness ?? session?.meta?.data?.harness ?? '';
  const harnessConfig = harness ? harnesses?.[harness] : undefined;
  // Latest event on top: the live tail reads newest-first without scrolling.
  // The input prompt is rendered as a first-class panel, not as a log row.
  // Carry each event's original (append-only) index so rows get a *stable* key:
  // keying by the reversed position would reassign every row's open/collapsed
  // state to a different event whenever a new event is prepended, which both
  // reopens rows the user had closed and breaks native scroll anchoring.
  const ordered = displayEvents
    ? displayEvents
        .map((event: any, idx: number) => ({ event, idx: event?._cursor ?? idx }))
        .filter(({ event }: any) => event.kind !== 'session_start' && event.kind !== 'session_meta')
        .reverse()
    : displayEvents;
  const role = session ? sessionRole(session) : '';
  const groups = role === 'subagent' ? null : groupSubagents(ordered);
  // Progressive rendering: paint the newest events immediately and stream the
  // rest in over the next frames, so a long session doesn't block on rendering
  // every event before anything shows. Reset the ramp when the session changes.
  //
  // Network pagination bounds this list. As an older page arrives, progressively
  // paint those newly loaded rows without remounting the already-visible tail.
  const renderList: any[] = groups ?? ordered ?? [];
  const shownCount = useProgressiveCount(renderList.length, { resetKey: selected, initial: 40, step: 100 });
  const rendering = renderList.length - shownCount;
  const title = session?.meta?.name ?? session?.session ?? 'Log';
  // The engine stamps the real model onto session_meta/usage events; fall back to
  // it so the model shows even when the config never pinned one (and live, before
  // the session meta is written at the end of the run).
  const observedModel = displayEvents?.find((event: any) => event?.data?.model)?.data?.model;
  // Only what this run actually recorded: session meta, or the model the engine
  // stamped onto its own event stream. Never the live harnessConfig — config.yaml
  // drifts over time, so backfilling a past run from it would mislabel it. When
  // nothing was captured, the chip simply doesn't render.
  const model = session?.meta?.model ?? session?.meta?.effective_model ?? observedModel;
  // Reasoning-effort tier the run used (Codex effort / Claude thinking budget),
  // stamped into session meta by the harness. No config fallback (see model above).
  const effort = session?.meta?.effort ?? session?.effort;
  // The session records the kind the engine ACTUALLY ran with; use only that, so a
  // past run isn't mislabeled by the current config (which may differ, or default
  // to claude-code). Blank when the run captured nothing.
  const kind = session?.meta?.harness_kind;
  const round = session?.meta?.round;
  const sessionId = session?.meta?.session_id ?? session?.meta?.data?.session_id;
  const fallbackReport = !report?.trim() ? latestReportFallback(events) : '';
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
              {effort && <span className="transcript-meta-chip" title="Reasoning-effort tier this run used">effort <strong>{effort}</strong></span>}
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
      {runId && session?.session && !session?.synthetic && (
        <SessionCommitsPanel runId={runId} session={session.session} />
      )}
      {recommendation && recommendation.trim() && (
        <details className="log-panel report-panel" open>
          <summary>Recommendation</summary>
          <div className="log-md"><MarkdownBlock content={recommendation} /></div>
        </details>
      )}
      {(report?.trim() || fallbackReport) && (
        <details className="log-panel report-panel" open>
          <summary>{report?.trim() ? 'Report' : 'Report fallback'}</summary>
          <div className="log-md"><MarkdownBlock content={report?.trim() ? report : fallbackReport} /></div>
        </details>
      )}
      {events === null && <p className="empty transcript-empty">{selected ? 'Loading session…' : 'Select a session to inspect its events.'}</p>}
      {events && events.length === 0 && <p className="empty transcript-empty">No events in this session.</p>}
      {run?.stop?.reason && (
        <div className="notice warning log-stop-note">
          Run stopped at round {run.stop.round ?? '?'}: {run.stop.reason}
        </div>
      )}
      <div className="log-lines">
        {groups ? groups.slice(0, shownCount).map((group, i) =>
          group.sub ? (
            <SubagentSublog key={`sub-${group.id}-${i}`} group={group} />
          ) : (
            <TranscriptEvent key={group.item.idx} event={group.item.event} forceOpen={null} />
          ),
        ) : ordered?.slice(0, shownCount).map(({ event, idx }: any) => (
          <TranscriptEvent key={idx} event={event} forceOpen={null} />
        ))}
        {rendering > 0 && (
          <p className="empty transcript-empty transcript-loading-more">Rendering {rendering} more event{rendering === 1 ? '' : 's'}…</p>
        )}
        {hasOlder && rendering === 0 && onLoadOlder && (
          <button type="button" className="log-expand-more" onClick={onLoadOlder} disabled={loadingOlder}>
            {loadingOlder ? 'Loading older entries…' : `Load ${EVENT_PAGE} older entries`}
            <span className="log-expand-rest">older events remain on disk</span>
          </button>
        )}
        {transcriptError && <p className="empty transcript-empty">{transcriptError}</p>}
      </div>
      {session && <SessionParameters session={session} harness={harness} harnessConfig={harnessConfig} nextSessionStart={nextSessionStart} tick={sessionKey(session) === activeTickSession} />}
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
  nextSessionStart,
  tick = false,
}: {
  session: any;
  harness?: string;
  harnessConfig?: any;
  nextSessionStart?: string;
  tick?: boolean;
}) {
  const meta = session.meta ?? {};
  const engineSession = meta.engine_session_id ?? meta.session_id ?? meta.data?.session_id;
  const durEnd = boundedSessionEnd(session, Date.now(), nextSessionStart, tick);
  const telemetry = session.telemetry ?? {};
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
    ['Session duration', formatDuration(session.started_at, durEnd)],
    ['Compactions', telemetry.compaction_count ? String(telemetry.compaction_count) : ''],
    ['Last compaction', formatDateTime(telemetry.last_compaction_at)],
    ['Current request input', telemetry.request_tokens_in ? `${Number(telemetry.request_tokens_in).toLocaleString()} tokens` : ''],
    ['Current request cached', telemetry.request_cached_tokens_in ? `${Number(telemetry.request_cached_tokens_in).toLocaleString()} tokens` : ''],
    ['Model context window', telemetry.model_context_window ? `${Number(telemetry.model_context_window).toLocaleString()} tokens` : ''],
    ['Cumulative engine input', telemetry.cumulative_tokens_in ? `${Number(telemetry.cumulative_tokens_in).toLocaleString()} tokens` : ''],
    ['Workspace SHA', shortSha(meta.workspace_sha)],
    ['Project SHAs', formatProjectRevisions(meta.project_revisions)],
    ['Harness', harness],
    ['Harness kind', session.meta?.harness_kind],
    // Only what this run recorded — never the live config (it drifts, so it would
    // mislabel a past run). Blank when the run captured nothing.
    ['Model', session.model ?? meta.model ?? meta.effective_model],
    ['Effort', session.effort ?? meta.effort],
    ['Auth', meta.auth],
    ['Config dir', meta.config_dir],
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
  thinking: '#7c3aed', text: '#2563eb', tool_call: '#d97706',
  tool_result: '#059669', error: '#dc2626', session_start: '#64748b', session_end: '#64748b',
  subagent_start: '#7c3aed', subagent_end: '#15803d',
  workflow_progress: '#0f766e',
  context: '#0f766e', compaction: '#b45309',
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
  subagent: '#7c3aed', inbox: '#0891b2', dag: '#d97706', blueprint: '#2563eb',
  search: '#0d9488', lean: '#15803d', git: '#9333ea', skill: '#b45309', lsp: '#059669', mcp: '#059669',
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
    if (/\bhorizon\s+graph\b/.test(cmd)) return 'dag';
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

function latestReportFallback(events: any[] | null): string {
  if (!events) return '';
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event?.kind !== 'text') continue;
    const text = stripAnsi(eventBody(event)).trim();
    if (text) return text;
  }
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

// ── Lean LSP MCP tools: render calls/results structurally instead of raw JSON ──
// The `lean-lsp` server drives the fast proof loop (goal / diagnostics /
// multi_attempt) and the search arsenal (leansearch / loogle / …). Showing these
// as compact cards makes that loop legible in the log.

// `mcp__lean-lsp__lean_goal` -> `lean_goal`; null for any other tool.
function leanLspTool(name: string): string | null {
  return name.startsWith('mcp__lean-lsp__') ? name.slice('mcp__lean-lsp__'.length) : null;
}

function fileRef(path: any, line?: any, column?: any): string {
  if (!path) return '';
  const base = String(path).split('/').pop() || String(path);
  const loc = [line, column].filter((x) => x != null && x !== '').join(':');
  return loc ? `${base}:${loc}` : base;
}

function LeanCallCard({ tool, input }: { tool: string; input: any }) {
  let main = '';
  let sub = '';
  let snippets: string[] | null = null;
  if (input.query != null) main = `"${String(input.query)}"`;
  else if (input.file_path != null || input.path != null)
    main = fileRef(input.file_path ?? input.path, input.line, input.column);
  if (input.theorem_name || input.declaration_name) sub = String(input.theorem_name ?? input.declaration_name);
  if (Array.isArray(input.snippets)) { snippets = input.snippets.map(String); sub = `${input.snippets.length} tactics`; }
  else if (input.code != null) snippets = [String(input.code)];
  return (
    <div className="lean-card">
      <div className="lean-card-head">
        <span className="lean-badge">lsp</span>
        <span className="lean-tool">{tool}</span>
        {main && <code className="lean-arg">{main}</code>}
        {sub && <span className="lean-sub">{sub}</span>}
      </div>
      {snippets && snippets.length > 0 && <pre className="log-code lean-snippets">{snippets.join('\n')}</pre>}
    </div>
  );
}

function LeanGoals({ goals }: { goals: any[] }) {
  if (!goals || goals.length === 0) return <div className="lean-ok">✓ no goals — complete</div>;
  return (
    <div className="lean-goals">
      {goals.map((g, i) => {
        const goalText = typeof g === 'string' ? g : String(g?.goal ?? '');
        const hyps: string[] = typeof g === 'object' && Array.isArray(g?.hypotheses) ? g.hypotheses : [];
        return (
          <div className="lean-goal" key={i}>
            {hyps.map((h, j) => <div className="lean-hyp" key={j}>{h}</div>)}
            <div className="lean-turnstile"><span className="lean-turn">⊢</span> {goalText}</div>
          </div>
        );
      })}
    </div>
  );
}

function diagSeverity(item: any): 'error' | 'warn' | 'info' {
  const s = typeof item === 'string' ? item : JSON.stringify(item ?? '');
  if (/severity['":\s]*1\b/.test(s)) return 'error';
  if (/severity['":\s]*2\b/.test(s)) return 'warn';
  return 'info';
}

function LeanDiagnostics({ result }: { result: any }) {
  const items: any[] = result.items ?? result.diagnostics ?? [];
  const failed: any[] = result.failed_dependencies ?? [];
  if ((!items || items.length === 0) && (!failed || failed.length === 0)) {
    return <div className="lean-ok">{result.success === false ? '✗ failed' : '✓ no diagnostics'}</div>;
  }
  return (
    <div className="lean-diags">
      {items.map((it, i) => (
        <div className={`lean-diag lean-diag-${diagSeverity(it)}`} key={i}>
          {typeof it === 'string' ? it : (it?.message ?? JSON.stringify(it))}
        </div>
      ))}
      {failed.map((d, i) => <div className="lean-diag lean-diag-error" key={`f${i}`}>failed dependency: {String(d)}</div>)}
    </div>
  );
}

function LeanResults({ items }: { items: any[] }) {
  return (
    <div className="lean-results">
      {items.slice(0, 20).map((it, i) => (
        <div className="lean-result" key={i}>
          <code className="lean-result-name">{String(it.name ?? '')}</code>
          {it.module && <span className="lean-result-mod">{String(it.module)}</span>}
          {(it.type || it.kind) && <code className="lean-result-type">{String(it.type ?? it.kind)}</code>}
        </div>
      ))}
      {items.length > 20 && <div className="lean-more">+{items.length - 20} more</div>}
    </div>
  );
}

function LeanAttempts({ rows }: { rows: any[] }) {
  return (
    <div className="lean-attempts">
      {rows.map((r, i) => {
        const ok = Array.isArray(r?.goals) ? r.goals.length === 0 : /no goals/.test(String(r?.goals ?? ''));
        return (
          <div className={`lean-attempt lean-attempt-${ok ? 'ok' : 'bad'}`} key={i}>
            <span className="lean-attempt-mark">{ok ? '✓' : '✗'}</span>
            <code className="lean-attempt-snip">{String(r?.snippet ?? '').trim()}</code>
          </div>
        );
      })}
    </div>
  );
}

function LeanPremises({ names }: { names: string[] }) {
  return (
    <div className="lean-premises">
      {names.slice(0, 40).map((n, i) => <code className="lean-premise" key={i}>{n}</code>)}
    </div>
  );
}

// Detect a lean-lsp result shape and render it; null → caller falls back to raw.
function renderLeanResult(text: string): React.ReactElement | null {
  let v: any;
  try { v = JSON.parse(text.trim()); } catch { return null; }
  if (v == null || typeof v !== 'object') return null;
  if (!Array.isArray(v)) {
    if (Array.isArray(v.goals_after) || Array.isArray(v.goals_before)) return <LeanGoals goals={v.goals_after ?? []} />;
    if (typeof v.success === 'boolean') return <LeanDiagnostics result={v} />;
    if (Array.isArray(v.items) && v.items[0] && typeof v.items[0] === 'object' && v.items[0].name) return <LeanResults items={v.items} />;
    return null;
  }
  if (v.length === 0) return null;
  if (typeof v[0] === 'object' && v[0] && 'snippet' in v[0]) return <LeanAttempts rows={v} />;
  if (typeof v[0] === 'string') return <LeanPremises names={v} />;
  if (typeof v[0] === 'object' && v[0] && v[0].name) return <LeanResults items={v} />;
  return null;
}

function ToolCallView({ event, text }: { event: any; text: string }) {
  const name = String(event.tool || '');
  const input = toolInput(event);
  const leanTool = leanLspTool(name);
  if (leanTool) return <LeanCallCard tool={leanTool} input={input} />;
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
  const lean = exitCode === undefined && text ? renderLeanResult(text) : null;
  return (
    <div className="log-tool-result">
      {exitCode !== undefined && <span className={`exit-code exit-${exitCode === 0 ? 'ok' : 'bad'}`}>exit {String(exitCode)}</span>}
      {lean ?? <pre className="log-output">{text || 'no output'}</pre>}
    </div>
  );
}

function SubagentLifecycleView({ event }: { event: any }) {
  const data = event.data ?? {};
  const started = event.kind === 'subagent_start';
  const name = String(data.name || 'subagent');
  const status = String(data.status || (started ? 'running' : 'closed'));
  const succeeded = status === 'completed';
  const duration = data.duration_seconds != null ? formatSeconds(Number(data.duration_seconds)) : '';
  return (
    <div className={`subagent-lifecycle-card ${started ? 'started' : 'ended'} status-${status}`}>
      <span className="subagent-lifecycle-glyph" aria-hidden="true">{started ? '↗' : succeeded ? '✓' : status === 'orphaned' ? '?' : '!'}</span>
      <div className="subagent-lifecycle-main">
        <strong>{name}</strong>
        <span>{started ? 'agent dispatched' : `agent ${status}`}</span>
        {data.summary && String(data.summary) !== event.text ? <small>{String(data.summary)}</small> : null}
      </div>
      <div className="subagent-lifecycle-meta">
        {data.nickname || data.agent_nickname ? <span>{String(data.nickname || data.agent_nickname)}</span> : null}
        {data.subagent_type ? <span>{String(data.subagent_type)}</span> : null}
        {data.model ? <span>{shortModel(String(data.model))}</span> : null}
        {duration ? <span>{duration}</span> : null}
      </div>
    </div>
  );
}

function ContextTelemetryView({ event }: { event: any }) {
  const data = event.data ?? {};
  const requestIn = Number(data.request_tokens_in ?? 0);
  const requestOut = Number(data.request_tokens_out ?? 0);
  const cached = Number(data.request_cached_tokens_in ?? 0);
  const cumulativeIn = Number(data.cumulative_tokens_in ?? 0);
  const windowSize = Number(data.model_context_window ?? 0);
  return (
    <div className="context-telemetry-card">
      <strong>Context snapshot</strong>
      <span>{requestIn.toLocaleString()} current input</span>
      {requestOut > 0 && <span>{requestOut.toLocaleString()} current output</span>}
      {cached > 0 && <span>{cached.toLocaleString()} cached input</span>}
      {windowSize > 0 && <span>{windowSize.toLocaleString()} context window</span>}
      {cumulativeIn > 0 && <span>{cumulativeIn.toLocaleString()} cumulative input</span>}
    </div>
  );
}

function CompactionView({ event }: { event: any }) {
  const data = event.data ?? {};
  const before = Number(data.tokens_before ?? data.before_tokens ?? 0);
  const after = Number(data.tokens_after ?? data.after_tokens ?? 0);
  return (
    <div className="compaction-card">
      <strong>Context compacted</strong>
      <span>{String(data.reason || data.trigger || 'automatic')}</span>
      {before > 0 && <span>{before.toLocaleString()} before</span>}
      {after > 0 && <span>{after.toLocaleString()} after</span>}
    </div>
  );
}

function compactCount(value: number): string {
  if (!Number.isFinite(value)) return '';
  return new Intl.NumberFormat(undefined, {
    notation: 'compact', maximumFractionDigits: 1,
  }).format(Math.max(0, value));
}

function WorkflowProgressView({ event }: { event: any }) {
  const data = event.data ?? {};
  const name = String(data.name || 'Workflow');
  const done = Number(data.completed_agents ?? 0);
  const total = Number(data.total_agents ?? 0);
  const status = String(data.status || 'running');
  const terminal = ['completed', 'failed', 'error', 'cancelled'].includes(status);
  const duration = data.duration_seconds != null ? formatSeconds(Number(data.duration_seconds)) : '';
  const tokens = Number(data.subagent_tokens ?? 0);
  const errors = Number(data.agents_error ?? 0);
  const progress = total > 0 ? Math.min(100, Math.max(0, (done / total) * 100)) : 0;
  return (
    <div className={`workflow-progress-card ${terminal ? 'terminal' : 'running'} ${errors ? 'has-errors' : ''}`}>
      <span className="workflow-progress-glyph" aria-hidden="true">{status === 'completed' && !errors ? '✓' : terminal || errors ? '!' : '○'}</span>
      <div className="workflow-progress-main">
        <div className="workflow-progress-title">
          <strong>{name}</strong>
          <span>{total > 0 ? `${done}/${total} agents done` : status}</span>
        </div>
        {data.summary ? <small>{String(data.summary)}</small> : null}
        {total > 0 ? (
          <div className="workflow-progress-track" aria-label={`${done} of ${total} agents done`}>
            <span style={{ width: `${progress}%` }} />
          </div>
        ) : null}
      </div>
      <div className="workflow-progress-meta">
        {duration ? <span>{duration}</span> : null}
        {tokens ? <span>{compactCount(tokens)} tokens</span> : null}
        {errors ? <span className="workflow-error-count">{errors} failed</span> : null}
        {data.agents_skipped ? <span>{String(data.agents_skipped)} skipped</span> : null}
      </div>
    </div>
  );
}

function EventBodyView({ event, text }: { event: any; text: string }) {
  if (event.kind === 'workflow_progress') {
    return <WorkflowProgressView event={event} />;
  }
  if (event.kind === 'subagent_start' || event.kind === 'subagent_end') {
    return <SubagentLifecycleView event={event} />;
  }
  if (event.kind === 'context') return <ContextTelemetryView event={event} />;
  if (event.kind === 'compaction') return <CompactionView event={event} />;
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

// Memoized so that progressively growing the rendered event count (see
// useProgressiveCount) doesn't re-render rows already on screen — event objects
// keep a stable identity across polls, so this keeps the whole list O(n).
const TranscriptEvent = React.memo(function TranscriptEvent({ event, forceOpen }: { event: any; forceOpen: boolean | null }) {
  const text = stripAnsi(eventBody(event)).trim();
  const hasBody = text.length > 0;
  const long = text.length > 280 || text.includes('\n');
  const [open, setOpen] = useState(!long);
  useEffect(() => { if (forceOpen !== null) setOpen(forceOpen); }, [forceOpen]);
  const color = event.kind === 'subagent_end' && event.data?.status !== 'completed'
    ? '#dc2626'
    : EVENT_COLORS[event.kind] ?? 'var(--text-muted)';
  const fullLabel = event.tool ? event.tool : event.kind;
  const label = event.tool
    ? shortTool(event.tool)
    : event.kind === 'session_start'
      ? 'input'
      : event.kind === 'subagent_start'
        ? 'agent start'
        : event.kind === 'subagent_end'
          ? 'agent end'
          : event.kind === 'workflow_progress'
            ? 'workflow'
          : event.kind;
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
    <div className={`log-line log-kind-${event.kind}`} onClick={toggle} style={{ cursor: long ? 'pointer' : 'default' }}>
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
});

// A run row that collapses to just its id (e.g. "0001") by default; clicking
// expands the whole session tree (all steps).
function TaskLinkChip({ taskId }: { taskId: string }) {
  return (
    <Link
      className="meta-chip task-chip"
      to={`/tasks?task=${encodeURIComponent(taskId)}`}
      title={`Open task ${taskId}`}
      onClick={(event) => event.stopPropagation()}
    >
      task {taskId}
    </Link>
  );
}

function taskIdsForRun(run: any): string[] {
  const ids = new Set<string>();
  const focus = run.focus ?? {};
  if (typeof focus.task === 'string' && focus.task) ids.add(focus.task);
  for (const taskId of focus.tasks ?? []) {
    if (taskId) ids.add(String(taskId));
  }
  for (const session of flattenSessions(run.sessions ?? [])) {
    const taskId = session.meta?.task_id;
    if (taskId) ids.add(String(taskId));
  }
  return [...ids];
}

function runStartAt(run: any): string | undefined {
  const sessions = flattenSessions(run.sessions ?? []);
  const sessionStarts = sessions.map((session: any) => session.started_at).filter(Boolean).sort();
  return run.created_at ?? sessionStarts[0];
}

function displayRunName(runId: string | number | undefined): string {
  const raw = String(runId ?? '');
  const n = Number.parseInt(raw, 10);
  return Number.isFinite(n) ? `run n°${n}` : `run ${raw}`;
}

function RunGroup({ run, selected, onSelect, now }: { run: any; selected: string; onSelect: (ref: string) => void; now: number }) {
  const [open, setOpen] = useState(false);
  const sessions = run.sessions ?? [];
  const taskIds = taskIdsForRun(run);
  const runDuration = runDurationSeconds(run, now);
  const activeTickSession = activeRunSessionKey(run);
  const toggle = (event: React.MouseEvent | React.KeyboardEvent) => {
    if ((event.target as HTMLElement).closest('a, button')) return;
    setOpen((v) => !v);
  };
  return (
    <div className="transcript-run-group">
      <div
        className="run-header"
        role="button"
        tabIndex={0}
        onClick={toggle}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            toggle(event);
          }
        }}
        aria-expanded={open}
      >
        <span className="ev-caret">{open ? '▾' : '▸'}</span>
        <strong>{displayRunName(run.id)}</strong>
        <StatusIcon value={run.status} />
        {taskIds.map((taskId) => <TaskLinkChip key={taskId} taskId={taskId} />)}
        {runDuration !== null && <span className="meta-chip">{formatSeconds(runDuration)}</span>}
        <span className="meta-chip">{run.session_count ?? 0} sessions</span>
        <UsageChips usage={run.usage} />
      </div>
      {open && (
        <div className="run-sessions-tree">
          <SelectableSessionTree sessions={sessions} selected={selected} onSelect={onSelect} now={now} fallbackTaskIds={taskIds} activeTickSession={activeTickSession} />
        </div>
      )}
    </div>
  );
}

function SelectableSessionTree({
  sessions,
  selected,
  onSelect,
  now,
  fallbackTaskIds = [],
  activeTickSession = '',
}: {
  sessions: any[];
  selected: string;
  onSelect: (ref: string) => void;
  now: number;
  fallbackTaskIds?: string[];
  activeTickSession?: string;
}) {
  if (sessions.length === 0) return null;
  const orderedSessions = [...sessions].sort(compareSessions);
  const nextStarts = nextSessionStartMap(sessions);
  return (
    <div className="selectable-tree">
      {orderedSessions.map((session) => (
        <SessionNode
          key={`${session.parent}-${session.session}`}
          session={session}
          selected={selected}
          onSelect={onSelect}
          now={now}
          fallbackTaskIds={fallbackTaskIds}
          nextSessionStart={nextStarts.get(session.session)}
          activeTickSession={activeTickSession}
        />
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

function SessionNode({
  session,
  selected,
  onSelect,
  now,
  fallbackTaskIds = [],
  nextSessionStart,
  activeTickSession = '',
}: {
  session: any;
  selected: string;
  onSelect: (ref: string) => void;
  now: number;
  fallbackTaskIds?: string[];
  nextSessionStart?: string;
  activeTickSession?: string;
}) {
  const hasChildren = (session.children?.length ?? 0) > 0;
  const round = typeof session.meta?.round === 'number' ? `r${session.meta.round}` : '';
  const role = sessionRole(session);
  const displayName = displaySessionName(session);
  const model = shortModel(session.model);
  const effort = session.effort ?? session.meta?.effort;
  const durEnd = boundedSessionEnd(session, now, nextSessionStart, sessionKey(session) === activeTickSession);
  const taskIds = session.meta?.task_id ? [String(session.meta.task_id)] : fallbackTaskIds;
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
            {effort && <span className="meta-chip effort-chip" title="Reasoning-effort tier">{effort}</span>}
            {round && <span className="meta-chip">{round}</span>}
            {session.started_at && <span className="meta-chip" title={`${formatTime(session.started_at)}-${formatTime(session.ended_at)}`}>{formatTime(session.started_at)}</span>}
            {session.started_at && <span className="meta-chip">{formatDuration(session.started_at, durEnd)}</span>}
            <UsageChips usage={session.usage} />
          </div>
        </button>
        {taskIds.map((taskId) => <TaskLinkChip key={taskId} taskId={taskId} />)}
      </div>
      {hasChildren && (
        <div className="session-children">
          <SelectableSessionTree sessions={session.children} selected={selected} onSelect={onSelect} now={now} fallbackTaskIds={taskIds} activeTickSession={activeTickSession} />
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
                <Badge>{displayRunName(run.id)}</Badge>
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

function roadmapCreatedAt(item: any): string | undefined {
  return item.activity?.created_at || item.metadata?.created_at;
}

function roadmapActivityAt(item: any): string | undefined {
  return item.activity?.updated_at || item.metadata?.updated_at || roadmapCreatedAt(item);
}

function roadmapActivityTitle(item: any): string {
  const taskCount = Number(item.activity?.task_count || 0);
  const sources = ['roadmap edits, comments, and history'];
  if (taskCount > 0) sources.push(`${taskCount} linked task${taskCount === 1 ? '' : 's'}`);
  if (item.activity?.updated_from_descendants) sources.push('child roadmap items');
  return `Latest activity across ${sources.join(', ')}`;
}

function formatCompactDateTime(value: string | undefined): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function RoadmapItemCard({ item, runAction, projects = [], parentOptions = [] }: { item: any, runAction?: any, projects?: string[], parentOptions?: string[] }) {
  const [editing, setEditing] = useState(false);
  const [visibleStatus, setVisibleStatus] = useState(item.status);
  const [priority, setPriority] = useState(item.priority || 'normal');
  const createdAt = roadmapCreatedAt(item);
  const activityAt = roadmapActivityAt(item);

  const updateStatus = (nextStatus: string) => {
    const previous = visibleStatus;
    setVisibleStatus(nextStatus);
    runAction?.({ action: 'status', id: item.id, status: nextStatus, author: 'human' }).then((ok: boolean) => {
      if (!ok) setVisibleStatus(previous);
    });
  };

  const updatePriority = (next: string) => {
    const previous = priority;
    setPriority(next);
    runAction?.({ action: 'edit', id: item.id, priority: next, author: 'human' }).then((ok: boolean) => {
      if (!ok) setPriority(previous);
    });
  };

  // Re-sync inline-editable fields when fresh props arrive (e.g. after another
  // copy of the same shared item is updated and the global state reloads).
  useEffect(() => {
    setVisibleStatus(item.status);
    setPriority(item.priority || 'normal');
  }, [item.status, item.priority]);

  const deleteItem = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    if (!window.confirm(`Delete roadmap item ${item.id}?`)) return;
    runAction?.({ action: 'delete', id: item.id, author: 'human' }, 'Roadmap item deleted.');
  };

  const editItem = (event: React.MouseEvent) => {
    event.preventDefault();
    event.stopPropagation();
    setEditing(true);
  };

  return (
    <details className="roadmap-card" open={editing}>
      <summary style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <div className="roadmap-title" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', flex: 1, minWidth: 0 }}>
          {!STATIC && runAction ? (
            <select className={`chip-select state ${visibleStatus}`} value={visibleStatus} onChange={(e) => updateStatus(e.target.value)}>
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
          {activityAt && (
            <time className="roadmap-activity-time" title={roadmapActivityTitle(item)}>
              activity {formatCompactDateTime(activityAt)}
            </time>
          )}
        </div>
        {!STATIC && runAction && (
          <div className="roadmap-row-actions">
            <button className="text-action roadmap-edit-action" type="button" onClick={editItem}>Edit</button>
            <button className="icon-danger button-reset" type="button" onClick={deleteItem} title="Delete roadmap item" aria-label="Delete roadmap item"><TrashIcon /></button>
          </div>
        )}
      </summary>
      <div className="roadmap-body">
        {editing ? (
          <RoadmapEditor
            item={item}
            runAction={runAction}
            projects={projects}
            parentOptions={parentOptions}
            onCancel={() => setEditing(false)}
            onSaved={() => setEditing(false)}
          />
        ) : (
          <div className="roadmap-summary">
             <div className="item-meta">
               <span>{item.id}</span>
               {item.metadata?.author && <span>by {item.metadata.author}</span>}
               <ProvenanceChip provenance={item.metadata?.provenance} />
               {createdAt && (
                 <span title={item.activity?.created_at_inferred ? 'Inferred from recorded roadmap/task activity' : undefined}>
                   created {formatDate(createdAt)}{item.activity?.created_at_inferred ? ' (inferred)' : ''}
                 </span>
               )}
               {activityAt && <span title={roadmapActivityTitle(item)}>latest activity {formatDate(activityAt)}</span>}
             </div>
             <div className="roadmap-fields roadmap-item-fields">
               <FieldChips label="Projects" values={item.projects ?? []} />
               <FieldChips label="Owner" values={item.metadata?.owner ? [item.metadata.owner] : []} />
               <FieldChips label="Milestone" values={item.metadata?.milestone ? [item.metadata.milestone] : []} />
               <FieldChips label="Parent" values={roadmapParent(item) ? [roadmapParent(item)] : []} />
               <FieldChips label="Kind" values={item.kind ? [item.kind] : []} />
               <FieldChips label="Depends on" values={item.depends_on ?? []} />
               <FieldChips label="Tasks" values={item.task_refs ?? []} />
               <FieldChips label="Inbox" values={item.inbox_refs ?? []} />
             </div>
             <ActivityTimeline
               description={item.summary ? { body: item.summary, author: item.metadata?.author, at: createdAt } : undefined}
               comments={item.metadata?.comments ?? []}
               history={historyOf(item)}
               editable={false}
               itemId={item.id}
               runAction={runAction}
             />
             {!item.summary && (item.metadata?.comments ?? []).length === 0 && historyOf(item).length === 0 && <p className="empty">No summary.</p>}
             {!STATIC && runAction && <CommentComposer id={item.id} runAction={runAction} />}
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

export function Badge({ children }: { children: React.ReactNode }) {
  return <span className="badge">{children}</span>;
}

// Render a single line of markdown (bold/italic/code/math/links) without the
// block <p> wrapper — for titles in lists and cards. Also linkifies local
// references (roadmap/task/inbox/node/commit) via the ref-link context.
export function InlineMarkdown({ content }: { content: string }) {
  const links = useRefLinks();
  const html = markdownToHtml(content ?? '', links?.resolve).replace(/^\s*<p>/, '').replace(/<\/p>\s*$/, '');
  const onClick = links ? refChipClickHandler(links.go) : undefined;
  return <span className="inline-md" onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}

// Compact status as a glyph: ✓ done, ✕ failed, ◌ running (spins),
// Ⅱ interrupted/paused (stopped early, not active), ⏱ timed out, ⧖ throttled
// (transient API backoff — not a crash), ○ otherwise.
function StatusIcon({ value, title }: { value: string; title?: string }) {
  const map: Record<string, { glyph: string; cls: string }> = {
    completed: { glyph: '✓', cls: 'ok' },
    done: { glyph: '✓', cls: 'ok' },
    failed: { glyph: '✕', cls: 'fail' },
    running: { glyph: '', cls: 'run' },
    interrupted: { glyph: 'Ⅱ', cls: 'interrupted' },
    cancelled: { glyph: 'Ⅱ', cls: 'interrupted' },
    orphaned: { glyph: '?', cls: 'interrupted' },
    // Not crashes: waited out a timeout, or backed off on a transient API error.
    timed_out: { glyph: '⏱', cls: 'timed-out' },
    throttled: { glyph: '⧖', cls: 'throttled' },
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
  const secs = durationSeconds(start, end);
  return secs === null ? '' : formatSeconds(secs);
}

function formatSeconds(secs: number): string {
  const safe = Math.max(0, Math.round(secs));
  if (safe < 60) return `${safe}s`;
  return `${Math.floor(safe / 60)}m ${safe % 60}s`;
}

function durationSeconds(start?: string, end?: string): number | null {
  if (!start || !end) return null;
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (!Number.isFinite(s) || !Number.isFinite(e)) return null;
  return Math.max(0, Math.round((e - s) / 1000));
}

function boundedSessionEnd(session: any, now: number, nextStart?: string, tick = false): string | undefined {
  if (session.ended_at) return session.ended_at;
  if (tick && session.status === 'running') return new Date(now).toISOString();
  const childStart = firstChildStart(session);
  if (childStart) return childStart;
  if (nextStart) return nextStart;
  return session.last_at;
}

function firstChildStart(session: any): string | undefined {
  const children = sessionsInRunOrder(session.children ?? []);
  return children.find((child: any) => child.started_at)?.started_at;
}

function sessionKey(session: any): string {
  return String(session?.ref || session?.session || '');
}

function isAgenticSession(session: any): boolean {
  const role = sessionRole(session);
  return role === 'ground' || role === 'horizon';
}

function latestAgenticSession(sessions: any[]): any | undefined {
  const agentic = sessionsInRunOrder(sessions).filter(isAgenticSession);
  return agentic[agentic.length - 1];
}

function activeRunSessionKey(run: any): string {
  if (!run || run.status !== 'running') return '';
  const session = latestAgenticSession(run.sessions ?? []);
  if (!session || session.status !== 'running') return '';
  return sessionKey(session);
}

function sessionOrderValue(session: any): number {
  const match = String(session.session ?? '').match(/^(\d+)/);
  if (match) return Number.parseInt(match[1], 10);
  return Date.parse(session.started_at ?? '') || 0;
}

function sessionsInRunOrder(sessions: any[]): any[] {
  return [...sessions].sort((a, b) => {
    const ao = sessionOrderValue(a);
    const bo = sessionOrderValue(b);
    if (ao !== bo) return ao - bo;
    return String(a.session).localeCompare(String(b.session), undefined, { numeric: true });
  });
}

function nextSessionStartMap(sessions: any[]): Map<string, string | undefined> {
  const ordered = sessionsInRunOrder(sessions);
  return new Map(ordered.map((session, index) => [String(session.session), ordered[index + 1]?.started_at]));
}

function nextSessionStart(sessions: any[], target: any): string | undefined {
  const direct = nextSessionStartMap(sessions).get(String(target.session));
  if (direct) return direct;
  for (const session of sessions ?? []) {
    const child = nextSessionStart(session.children ?? [], target);
    if (child) return child;
  }
  return undefined;
}

function runDurationSeconds(run: any, now: number): number | null {
  const sessions = sessionsInRunOrder(run.sessions ?? []);
  if (!sessions.length) return null;
  const nextStarts = nextSessionStartMap(sessions);
  const active = activeRunSessionKey(run);
  let total = 0;
  let seen = false;
  for (const session of sessions) {
    const secs = durationSeconds(
      session.started_at,
      boundedSessionEnd(session, now, nextStarts.get(String(session.session)), sessionKey(session) === active),
    );
    if (secs !== null) {
      total += secs;
      seen = true;
    }
  }
  return seen ? total : null;
}

function formatDateTime(value: string | undefined): string {
  if (!value) return '';
  const d = new Date(value);
  return Number.isNaN(d.valueOf()) ? value : d.toLocaleString([], { hour12: false });
}

export function Status({ value, label }: { value: string; label?: string }) {
  return <span className={`status status-${value}`}>{label ?? value}</span>;
}

function EmptyRow({ colSpan, label }: { colSpan: number; label: string }) {
  return <tr><td colSpan={colSpan}><span className="empty">{label}</span></td></tr>;
}

function compareInboxItems(a: any, b: any) {
  const priority = (item: any) => item.kind === 'protection'
    ? 3
    : (item.kind === 'conversation' || item.metadata?.conversation ? 2 : 1);
  const priorityDifference = priority(b) - priority(a);
  if (priorityDifference) return priorityDifference;
  const aTime = Date.parse(inboxActivityAt(a) || '') || 0;
  const bTime = Date.parse(inboxActivityAt(b) || '') || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(a.id).localeCompare(String(b.id));
}

function compareTasks(a: any, b: any) {
  const aTime = Date.parse(taskActivityAt(a) || '') || 0;
  const bTime = Date.parse(taskActivityAt(b) || '') || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(a.id).localeCompare(String(b.id));
}

function compareRuns(a: any, b: any) {
  const aTime = Date.parse(runActivityAt(a) || '') || 0;
  const bTime = Date.parse(runActivityAt(b) || '') || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(b.id).localeCompare(String(a.id), undefined, { numeric: true });
}

function compareSessions(a: any, b: any) {
  const aTime = Date.parse(sessionActivityAt(a) || '') || 0;
  const bTime = Date.parse(sessionActivityAt(b) || '') || 0;
  if (aTime !== bTime) return bTime - aTime;
  return String(b.session).localeCompare(String(a.session), undefined, { numeric: true });
}

function taskActivityAt(task: any): string | undefined {
  return task.updated_at ?? task.metadata?.updated_at ?? task.created_at ?? task.metadata?.created_at;
}

function inboxActivityAt(item: any): string | undefined {
  return item.updated_at ?? item.created_at;
}

function runActivityAt(run: any): string | undefined {
  const sessions = flattenSessions(run.sessions ?? []);
  const sessionTimes = sessions.flatMap((session: any) => [session.last_at, session.ended_at, session.started_at]).filter(Boolean);
  const sortedSessionTimes = sessionTimes.sort();
  return run.updated_at ?? run.ended_at ?? run.created_at ?? sortedSessionTimes[sortedSessionTimes.length - 1];
}

function sessionActivityAt(session: any): string | undefined {
  return session.last_at ?? session.ended_at ?? session.started_at;
}

function flattenSessions(sessions: any[]): any[] {
  return sessions.flatMap((session) => [session, ...flattenSessions(session.children ?? [])]);
}

function normalizedInboxStatus(status: string | undefined) {
  if (status === 'completed') return 'closed';
  if (status === 'closed') return 'closed';
  if (status === 'archived') return 'archived';
  return status || 'open';
}

function filterLabel(value: string) {
  if (value === INBOX_WORKSPACE_SCOPE) return 'Workspace';
  if (value === 'completed') return 'closed';
  if (value === 'archived') return 'archived';
  if (value === 'accept') return gateLabel(value);
  if (value === 'pending') return gateLabel(value);
  if (value === 'reject') return gateLabel(value);
  if (value === 'clear') return gateLabel(value);
  return value;
}

function filterToken(value: string | undefined) {
  return filterLabel(String(value || '')).toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
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

function scopeTargets(item: any, key: string): string[] {
  const values = item?.scope?.[key];
  if (!Array.isArray(values)) return [];
  return values.map((value: any) => {
    if (typeof value === 'string') return value.trim();
    return String(value?.target ?? value?.id ?? value?.name ?? '').trim();
  }).filter(Boolean);
}

function inboxAuthor(item: any) {
  for (const value of [item.author, item.metadata?.author]) {
    if (typeof value === 'string' && value.trim()) return value.trim();
    if (value?.login) return String(value.login).trim();
  }
  return '';
}

// The finer-grained sub-identity behind a role author (e.g. the subagent
// descriptor name), kept in metadata so the author itself stays a clean role.
function inboxAgent(item: any) {
  const agent = item.metadata?.agent;
  return typeof agent === 'string' && agent.trim() ? agent.trim() : '';
}

function inboxAudienceTargets(item: any): string[] {
  return String(item?.audience || '')
    .split(',')
    .map((target) => target.trim())
    .filter(Boolean);
}

function inboxParticipants(item: any): string[] {
  const values = [...inboxAudienceTargets(item)];
  const raw = item?.metadata?.participants;
  if (Array.isArray(raw)) values.push(...raw.map(String));
  else if (typeof raw === 'string') values.push(...raw.split(','));
  const startedBy = String(item?.metadata?.started_by || '').trim();
  if (startedBy) values.push(startedBy);
  const provenance = item?.metadata?.provenance;
  const task = String(provenance?.task || '').trim();
  const run = String(provenance?.run || '').trim();
  if (task) values.push(`task:${task}`);
  else if (run) values.push(`run:${run}`);
  const author = inboxAuthor(item).toLowerCase();
  if (author === 'human') values.push('human');
  else if (author === 'horizon' && !startedBy && !task && !run) values.push('horizon');
  return [...new Set(values.map((value) => value.trim()).filter(Boolean))];
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
  filters: {
    query: string;
    providerFilter: Set<string>;
    statusFilter: Set<string>;
    gateFilter: Set<string>;
    kinds?: Set<string>;
    audienceFilter?: Set<string>;
    ownerFilter?: Set<string>;
    projectFilter?: Set<string>;
    authorFilter?: Set<string>;
  },
) {
  if (!filters.providerFilter.has(item.provider)) return false;
  if (!filters.statusFilter.has(normalizedInboxStatus(item.status))) return false;
  if (filters.kinds && filters.kinds.size > 0) {
    const kind = item.kind === 'conversation' || item.metadata?.conversation
      ? 'conversation'
      : item.kind;
    if (!filters.kinds.has(kind)) return false;
  }
  const audiences = inboxAudienceTargets(item);
  if (
    filters.audienceFilter
    && !(audiences.length ? audiences : ['general']).some((target) => filters.audienceFilter!.has(target))
  ) return false;
  if (filters.ownerFilter && !filters.ownerFilter.has(inboxOwnerTask(item) || 'shared')) return false;
  const projects = scopeTargets(item, 'projects');
  const projectScopes = projects.length ? projects : [INBOX_WORKSPACE_SCOPE];
  if (filters.projectFilter && filters.projectFilter.size > 0 && !projectScopes.some((project) => filters.projectFilter!.has(project))) return false;
  if (filters.authorFilter && filters.authorFilter.size > 0 && !filters.authorFilter.has(inboxAuthor(item))) return false;
  const gate = inboxGate(item.labels ?? []);
  if (!filters.gateFilter.has(gate)) return false;
  const query = filters.query.trim().toLowerCase();
  if (!query) return true;
  const comments = inboxComments(item).map((comment: any) => comment.body ?? '').join(' ');
  const haystack = [
    item.id,
    item.provider,
    item.kind,
    item.status,
    item.body,
    item.audience,
    inboxAuthor(item),
    inboxOwnerTask(item),
    ...inboxReadBy(item),
    ...projects,
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
