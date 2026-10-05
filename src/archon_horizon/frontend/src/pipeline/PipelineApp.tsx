import { lazy, Suspense, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { QueryClientProvider, useQueryClient } from "@tanstack/react-query";
import { Activity, Bot, ChevronRight, GitBranch, GitPullRequest, Home, LayoutGrid, ListTodo, Map as MapIcon, MessagesSquare, Network, RefreshCw, Search, Server, Users, X } from "lucide-react";
import { version as APP_VERSION } from "../../package.json";
import { ApiError, request, type Account, type Project, type Resources, type Run } from "./api";
import { queryClient, clearPendingCommand, useCommand, usePagedRead, useRead, useUpdates } from "./queries";
import "./pipeline.css";
import "../platform.css";
import "../projectDocuments.css";
import "./Desktop.css";

const Projects = lazy(() => import("./DesktopProjects"));
const RunActivity = lazy(() => import("./DesktopActivity"));
const Hosts = lazy(() => import("./DesktopAdministration").then(module => ({default: module.DesktopHosts})));
const Agents = lazy(() => import("./DesktopAdministration").then(module => ({default: module.DesktopAgents})));
const Accounts = lazy(() => import("./DesktopAdministration").then(module => ({default: module.DesktopAccounts})));
const SearchView = lazy(() => import("./DesktopSearch"));
const NativeView = lazy(() => import("./DesktopNative"));
const ProjectCreate = lazy(() => import("./Administration").then(module => ({default: module.ProjectCreate})));
const ProjectSettings = lazy(() => import("./Administration").then(module => ({default: module.ProjectSettings})));

const navigation = [
  {id: "projects", Icon: LayoutGrid, label: "Projects"},
  {id: "hosts", Icon: Server, label: "Execution hosts"},
  {id: "agents", Icon: Bot, label: "Agents"},
  {id: "search", Icon: Search, label: "Search"},
  {id: "forge", Icon: GitPullRequest, label: "Forge"},
  {id: "zulip", Icon: MessagesSquare, label: "Zulip"},
  {id: "activity", Icon: Activity, label: "Activity"},
  {id: "accounts", Icon: Users, label: "Accounts"},
];
const projectViewNames: Record<string, string> = {overview: "Overview", roadmap: "Objectives", graph: "Graph", nodes: "Nodes", node: "Home", "node-dag": "DAG", missions: "Missions"};

function locationState() {
  const values = new URLSearchParams(location.search);
  if (!values.has("tab")) {
    const aliases: Record<string, string> = {work: "activity", changes: "forge", discussions: "zulip", resources: "hosts", settings: "agents"};
    const previous = aliases[values.get("view") || ""];
    values.set("tab", previous || "projects");
    if (previous) values.delete("view");
  }
  if (!values.has("session") && values.has("assignment")) values.set("session", values.get("assignment")!);
  values.delete("assignment");
  return values;
}

export default function PipelineApp() {
  return <QueryClientProvider client={queryClient}><Entry /></QueryClientProvider>;
}

function Entry() {
  const account = useRead<Account>("session", "account", "global", "/auth/me");
  const client = useQueryClient();
  useEffect(() => {
    if (account.error instanceof ApiError && account.error.status === 401) client.removeQueries({predicate: query => query.queryKey[0] === "pipeline" && query.queryKey[1] !== "session"});
  }, [account.error, client]);
  if (account.error instanceof ApiError && account.error.status === 401) return <Login onSuccess={() => {client.clear(); void account.refetch();}} />;
  return <Shell account={account.data} error={account.error} reload={() => void account.refetch()} />;
}

function Login({onSuccess}: {onSuccess: () => void}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {await request("/auth/login", {method: "POST", body: JSON.stringify({username: form.get("username"), password: form.get("password")})}); onSuccess();}
    catch (failure) {setError(failure instanceof Error ? failure.message : String(failure));}
    finally {setBusy(false);}
  };
  return <main className="platform-auth"><div className="platform-auth-box"><div className="platform-brand"><strong>Archon Horizon</strong></div><h1>Sign in</h1><form onSubmit={event => void submit(event)}><label>Username<input name="username" autoComplete="username" required autoFocus /></label><label>Password<input name="password" type="password" autoComplete="current-password" required /></label>{error && <div className="platform-error" role="alert">{error}</div>}<button className="platform-primary" disabled={busy}>{busy ? "Signing in..." : "Sign in"}</button></form></div></main>;
}

function Shell({account, error, reload}: {account?: Account; error: Error | null; reload: () => void}) {
  const [url, setUrl] = useState(locationState);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState(false);
  const [opened, setOpened] = useState(() => new Set([url.get("tab")]));
  const client = useQueryClient();
  const accountId = account?.id || "";
  const admin = account?.permissions?.admin ?? account?.role === "admin";
  const projects = usePagedRead<Project>(accountId, "projects", "global", "/projects?limit=100", !!account);
  const projectItems = projects.data?.pages.flatMap(page => page.items) || [];
  const projectId = url.get("project") || "";
  const projectQuery = useRead<Project>(accountId, "projects", "global", `/projects/${encodeURIComponent(projectId)}`, !!account && !!projectId && !projectItems.some(item => item.id === projectId));
  if (projectQuery.data && !projectItems.some(item => item.id === projectQuery.data!.id)) projectItems.push(projectQuery.data);
  const project = projectItems.find(item => item.id === projectId);
  const resources = useRead<Resources>(accountId, "resources", "global", "/resources", !!account && admin);
  // The Activity reader owns this request; observe its cache for command scoping.
  const selectedRun = useRead<Pick<Run, "project_id">>(accountId, "assignments", "global",
    `/dashboard/activity/runs/${encodeURIComponent(url.get("run") || "")}?compact=true`, false);
  const connected = useUpdates(accountId, projectId);
  const commands = useCommand(accountId, projectId || selectedRun.data?.project_id || "", connected, account?.max_offline_replay_seconds);
  const writable = connected && !commands.busy && !!account && (account.permissions?.write ?? ["operator", "maintainer", "admin"].includes(account.role));
  const tab = navigation.some(item => item.id === url.get("tab")) ? url.get("tab")! : "projects";
  const view = url.get("view") || "overview";
  const inProject = tab === "projects" && !!projectId;
  const inNode = inProject && ["node", "node-dag"].includes(view);
  const navigate = (values: Record<string, string>, replace = false) => {
    const next = new URLSearchParams(url);
    Object.entries(values).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key));
    history[replace ? "replaceState" : "pushState"]({}, "", `/pipeline?${next}`);
    setUrl(next);
    if (values.tab) setOpened(previous => new Set([...previous, values.tab]));
  };
  useEffect(() => {
    const pop = () => {const next = locationState(); setUrl(next); setOpened(previous => new Set([...previous, next.get("tab")]));};
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
  const selectTab = (next: string) => navigate({tab: next, project: "", view: "", node: "", objective: "", run: "", session: "", q: ""});
  const projectView = (next: string) => navigate({view: next, node: "", objective: "", q: ""});
  const contextualNavigation = inProject ? inNode ? [
    {id: "back", Icon: ChevronRight, label: url.get("objective") ? "Back to roadmap" : "Back to nodes", action: () => navigate({view: url.get("objective") ? "roadmap" : "nodes", node: ""})},
    {id: "node", Icon: Home, label: "Home", action: () => navigate({view: "node"})},
    {id: "node-dag", Icon: GitBranch, label: "DAG", action: () => navigate({view: "node-dag"})},
  ] : [
    {id: "back", Icon: ChevronRight, label: "Back to projects", action: () => selectTab("projects")},
    {id: "overview", Icon: LayoutGrid, label: "Overview", action: () => projectView("overview")},
    {id: "roadmap", Icon: MapIcon, label: "Objectives", action: () => projectView("roadmap")},
    {id: "nodes", Icon: Network, label: "Nodes", action: () => projectView("nodes")},
    {id: "missions", Icon: ListTodo, label: "Missions", action: () => projectView("missions")},
  ] : navigation.filter(item => item.id !== "accounts" || admin).map(item => ({...item, action: () => selectTab(item.id)}));
  const heading = navigation.find(item => item.id === tab)?.label;
  const refresh = () => {reload(); void client.invalidateQueries({queryKey: ["pipeline", accountId]});};
  const signOut = async () => {
    try {await request("/auth/logout", {method: "POST"}); clearPendingCommand(); client.clear(); location.assign("/pipeline");}
    catch (failure) {window.alert(failure instanceof Error ? failure.message : String(failure));}
  };
  const failure = error || projects.error || projectQuery.error;
  return <div className="platform-shell horizon-desktop">
    <header className="platform-topbar"><div className="platform-brand"><strong>Archon Horizon</strong><span className="version-badge">v{APP_VERSION}</span><span className="platform-divider"/><span className="desktop-breadcrumb">{inProject && project && <><button className="platform-text-button desktop-project-context" title={project.title} onClick={() => projectView("overview")}>{project.title}</button><ChevronRight size={13}/></>}<span className="platform-context">{inProject ? projectViewNames[view] || "Project" : heading}</span></span></div><div className="platform-top-actions">{account && <span className="platform-context">{account.username} · {account.role}</span>}<span className={`platform-connection ${connected ? "" : "disconnected"}`} role="status"><i/>{connected ? "Connected" : "Reconnecting"}</span>{account && <button className="platform-text-button" onClick={() => void signOut()}>Sign out</button>}<button className="platform-icon-button" title="Refresh" aria-label="Refresh" onClick={refresh}><RefreshCw size={16}/></button></div></header>
    <div className="platform-body"><aside className="platform-sidebar"><div className="platform-nav-label">{inProject ? inNode ? "Node" : "Project" : "Horizon"}</div>{contextualNavigation.map(({id, Icon, label, action}) => <button key={id} title={label} className={`platform-nav-item ${(inProject ? view === id : tab === id) ? "active" : ""} ${id === "back" ? "platform-nav-back" : ""}`} aria-current={(inProject ? view === id : tab === id) ? "page" : undefined} onClick={action}><Icon size={17} className={id === "back" ? "platform-nav-back-icon" : undefined}/><span>{label}</span></button>)}<div className="platform-sidebar-foot"><span className="platform-online-dot"/>{resources.data ? `${resources.data.hosts.length} hosts` : "Archon Horizon"}</div></aside>
      <main className={`platform-main ${["forge", "zulip"].includes(tab) ? "platform-main-forge" : ""}`}>
        {!["forge", "zulip"].includes(tab) && !inProject && <div className="platform-main-head"><h1>{heading}</h1></div>}
        {failure && <div className="platform-error" role="alert">{failure.message}</div>}
        {commands.error && <div className="platform-error" role="alert">{commands.error}{commands.operation?.status === "uncertain" && <div className="desktop-command-actions"><button className="platform-small-button" onClick={() => void commands.reconcile(commands.operation!.id)}>Check operation</button><button className="platform-small-button" disabled={!commands.retryAllowed} onClick={() => void commands.retry()}>Retry same operation</button></div>}</div>}
        {!account ? <div className="platform-empty">{error ? "The control plane is unavailable." : "Connecting..."}</div> : <Suspense fallback={<div className="platform-empty">Loading...</div>}>
          {tab === "projects" && <Projects accountId={accountId} projects={projectItems} projectId={projectId} view={view} selection={{node: url.get("node") || undefined, objective: url.get("objective") || undefined}} writable={writable} admin={admin} onNavigate={navigate} onCreate={() => setCreating(true)} onEditProject={() => setEditing(true)}/>}
          {tab === "projects" && !projectId && projects.hasNextPage && <button className="platform-small-button" onClick={() => void projects.fetchNextPage()}>More projects</button>}
          {tab === "activity" && <RunActivity accountId={accountId} projects={projectItems} projectId={projectId} runId={url.get("run") || ""} sessionId={url.get("session") || ""} writable={writable} command={commands.execute} onNavigate={navigate}/>}
          {tab === "hosts" && <Hosts {...{accountId, admin, writable, projectId}}/>}
          {tab === "agents" && <Agents {...{accountId, admin, writable, projectId}}/>}
          {tab === "accounts" && <Accounts {...{accountId, admin, writable, projectId, account}}/>}
          {tab === "search" && <SearchView {...{accountId, projectId}} projects={projectItems}/>}
          {(["forge", "zulip"] as const).filter(kind => opened.has(kind) || tab === kind).map(kind => <div key={kind} hidden={tab !== kind}><NativeView {...{accountId}} projectId={projectId || projectItems[0]?.id || ""} kind={kind}/></div>)}
          {creating && <ProjectCreate accountId={accountId} writable={writable} onClose={() => setCreating(false)} onCreated={created => {setCreating(false); void projects.refetch(); navigate({tab: "projects", project: created.id, view: "overview", node: "", objective: ""});}}/>}
          {editing && project && <ProjectEdit title={project.title} onClose={() => setEditing(false)}><ProjectSettings {...{accountId, projectId, writable}}/></ProjectEdit>}
        </Suspense>}
      </main>
    </div>
  </div>;
}

function ProjectEdit({title, children, onClose}: {title: string; children: ReactNode; onClose: () => void}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {dialog.current?.showModal(); return () => dialog.current?.close();}, []);
  return <dialog ref={dialog} className="platform-modal desktop-project-dialog" onCancel={onClose}><div className="platform-modal-head"><h2>{title}</h2><button className="platform-icon-button" aria-label="Close project settings" onClick={onClose}><X size={17}/></button></div>{children}</dialog>;
}
