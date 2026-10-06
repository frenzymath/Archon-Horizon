import { createContext, lazy, Suspense, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps, CSSProperties, ReactNode } from "react";
import {
  Activity, ArrowLeft, ArrowUpRight, AtSign, Bell, Bot, Check, ChevronDown, ChevronRight,
  Circle, CircleAlert, Clock3, FileText, GitBranch, GitCommitHorizontal,
  GitPullRequest, Layers, LoaderCircle, MessagesSquare, Network, RefreshCw, RotateCcw, ScrollText, Search, Square, Target, Webhook, X,
} from "lucide-react";
import { request } from "../pipeline/api";
import RunPhase, { type Phase } from "../pipeline/RunPhase";
import { capabilityHref, capabilityReference } from "../utils/capabilityLinks";
import { dashboardUrl } from "../utils/navigation";
import { workspaceRepository } from "../utils/horizonReferences";
import { activityContextNodes, activityEventDetails, activityEventTitle, activityNodeLink, activityNodes, activityNoticeSummary, activityNotices, gitTransferBursts } from "../utils/activityPresentation";
import "./activity.css";

const DocumentView = lazy(() => import("./DocumentView"));
const ActivityDocumentReference = lazy(() => import("./ActivityDocumentReference"));
export type ActivityDataSource = {
  basePath: string;
  request: <T>(path: string, init?: RequestInit, timeoutMs?: number) => Promise<T>;
  cached?: <T>(path: string) => T | undefined;
  writable?: boolean;
  refreshVersion?: number;
  location?: { runId: string; sessionId: string };
  onNavigate?: (values: Record<string, string>, replace?: boolean) => void;
};
const ActivitySource = createContext<ActivityDataSource>({ basePath: "/dashboard/activity", request, writable: false });
function ActivityMarkdown(props: ComponentProps<typeof DocumentView>) {
  return <DocumentView {...props} />;
}
type Usage = { tokens_in: number | null; tokens_out: number | null; cost_usd: number | null;
  incomplete?: boolean;
  recorded?: Record<string, number | null>; coverage?: Record<string, number>; attempt_count?: number;
  estimated_cost_usd?: number | null; estimated_cost_coverage?: number };
type Context = Record<string, unknown>;
type Admission = {summary?: string; state?: "ready" | "waiting" | "unknown"; reason?: string;
  checked_at?: string; not_before?: string; expires_at?: string; next_check_at?: string};

function sessionAdmission(session: ActivitySession): Admission {
  const value = session.context?.admission;
  return value && typeof value === "object" ? value as Admission : {};
}

export function queuedSessions(sessions: ActivitySession[]) {
  return sessions.filter(session => !session.native && session.status === "queued")
    .sort((a, b) => (a.queue_position ?? Number.MAX_SAFE_INTEGER) - (b.queue_position ?? Number.MAX_SAFE_INTEGER)
      || a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
}

export function AdmissionDetails({admission}: {admission: Admission}) {
  return <span className="activity-admission-details">
    <strong className={`activity-admission-state ${admission.state || "unknown"}`}>
      {admission.state === "ready" ? "Ready for a slot" : admission.state === "waiting" ? "Waiting" : "Status unavailable"}
    </strong>
    <span>{admission.summary || "Condition details unavailable"}</span>
    {admission.reason && <span>{admission.reason}</span>}
    {admission.not_before && <span>Earliest start: {timestamp(admission.not_before)}</span>}
    {admission.expires_at && <span>Start deadline: {timestamp(admission.expires_at)}</span>}
    {admission.checked_at && <small>Checked {timestamp(admission.checked_at, true)}</small>}
  </span>;
}

export function sessionStatus(session: ActivitySession) {
  return session.status === "queued" && session.context?.retained_continuation === true
    ? "waiting to resume" : session.status;
}
export type ActivityExecution = {
  default_harness?: string;
  max_concurrent_runs?: number;
  hosts?: Record<string, { max_concurrent_runs?: number; harness_limits?: Record<string, number> }>;
};
export type ActivityRun = {
  id: string; number: number; title: string; project_id: string; project_title?: string; status: string;
  phase?: Phase;
  created_at: string; started_at?: string; finished_at?: string; session_count: number;
  running_session_count?: number; queued_session_count?: number;
  main_session_count?: number; subagent_count?: number; total_session_count?: number; stop_reason?: string;
  usage: Usage; elapsed_seconds: number; agent_seconds: number; models: string[]; context: Context;
  delegated_session_count?: number; native_subagent_count?: number;
  efforts?: string[];
  execution?: ActivityExecution;
  slot_capacity?: number | null;
  metrics_pending?: boolean;
};
export type ActivitySession = {
  id: string; run_id: string; parent_session_id?: string | null; label: string;
  title: string; role: string; functions?: string[]; status: string; created_at: string; started_at?: string;
  finished_at?: string; usage: Usage; models: string[]; skills: string[];
  host_id?: string; profile_id?: string; elapsed_seconds: number; agent_seconds: number;
  attempt_count: number; context: Context;
  native?: boolean;
  subagents?: {id: string; title: string; description: string; status: string; descriptor_revision?: number | null}[];
  session_number?: number; efforts?: string[];
  queue_position?: number;
  recovery?: { state?: string; reason?: string; retry_at?: number };
  can_resume?: boolean;
};
type Attempt = {
  id: string; number: number; status: string; host_id?: string; profile_id?: string;
  model?: string; effort?: string; started_at?: string; finished_at?: string; error?: string;
};
type Report = { id: string; revision: number; kind: string; label?: string; markdown: string; created_at: string };
export type ActivityEvent = {
  id: string; kind: string; title: string; created_at: string;
  sort_at?: string;
  actor?: string; run_id?: string; session_id?: string; attempt_id?: string | null;
  project_id?: string;
  detail_path?: string;
  links: { label: string; url: string }[]; data: Context;
};
type RunDetail = ActivityRun & { sessions: ActivitySession[]; sessions_next_before?: string | null; caused_by?: Context };
type SessionDetail = ActivitySession & {
  attempts: Attempt[]; reports: Report[]; events: ActivityEvent[]; next_before: number | string | null;
};
type DebugLogPage = { enabled: boolean; records: Record<string, unknown>[] };
type RunPage = { runs: ActivityRun[]; next_before: number | string | null };
type EventPage = { events: ActivityEvent[]; next_before: number | string | null };
type Project = { id: string; title?: string };

const isActive = (status: string) => ["queued", "running", "waiting", "cancelling"].includes(status);
export function sessionCounts(sessions: ActivitySession[]) {
  return {
    running: sessions.filter((session) => session.status === "running").length,
    queued: sessions.filter((session) => session.status === "queued" || session.status === "waiting").length,
    total: sessions.length,
  };
}

function sessionProfile(session: ActivitySession): string {
  if (session.functions?.includes("orchestrator")) return "Orchestrator";
  if (session.native) return "Native subagent";
  if (session.parent_session_id) return "Delegated session";
  return session.role;
}

function slotCount(value: unknown): number {
  const slots = typeof value === "number" ? value : Number(value);
  return Number.isFinite(slots) && slots > 0 ? Math.floor(slots) : 0;
}

export function runSlotCapacity(execution?: ActivityExecution | null): number | undefined {
  const hosts = execution?.hosts;
  let total = 0;
  if (hosts) {
    for (const host of Object.values(hosts)) {
      const limits = host?.harness_limits || {};
      let hostSlots = 0;
      for (const value of Object.values(limits)) hostSlots += slotCount(value);
      const hostCap = slotCount(host?.max_concurrent_runs);
      total += hostCap ? (hostSlots ? Math.min(hostSlots, hostCap) : hostCap) : hostSlots;
    }
  }
  const runCap = slotCount(execution?.max_concurrent_runs);
  if (runCap) total = total ? Math.min(total, runCap) : runCap;
  return total > 0 ? total : undefined;
}
const timestamp = (value?: string, seconds = false) => value ? new Date(value).toLocaleString([], {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}),
}) : "Not started";
export function activityDuration(seconds: number | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "Not recorded";
  const total = Math.max(0, Math.floor(seconds));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
}
const tokens = (value: number | null | undefined) => value == null ? "Not recorded" : value.toLocaleString();
const cost = (value: number | null | undefined) => value == null ? "Not recorded" : new Intl.NumberFormat(undefined, {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: value > 0 && value < 0.01 ? 4 : 2,
}).format(value);

export function activityLink(url: string): string | undefined {
  try {
    const parsed = new URL(url, "https://horizon.invalid");
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return undefined;
    return url;
  } catch {
    return undefined;
  }
}

// Missing parents and cycles can appear in imported history. Keep every session reachable.
export function sessionTree(sessions: ActivitySession[], collapsed: Set<string> = new Set(), filter: "all" | "active" = "all") {
  const visible = filter === "active" ? sessions.filter((session) => isActive(session.status)) : sessions;
  const ids = new Set(visible.map((session) => session.id));
  const children = new Map<string, ActivitySession[]>();
  const recency = (session: ActivitySession) => [session.created_at, session.started_at, session.finished_at]
    .filter((value): value is string => !!value).sort().pop() || "";
  const newestFirst = (left: ActivitySession, right: ActivitySession) => {
    return recency(right).localeCompare(recency(left));
  };
  for (const session of visible) {
    const parent = session.parent_session_id && ids.has(session.parent_session_id) ? session.parent_session_id : "";
    const group = children.get(parent) ?? [];
    group.push(session);
    children.set(parent, group);
  }
  for (const group of children.values()) group.sort(newestFirst);
  const seen = new Set<string>();
  const rows: { session: ActivitySession; depth: number; hasChildren: boolean }[] = [];
  const visit = (session: ActivitySession, depth: number, visible: boolean) => {
    if (seen.has(session.id)) return;
    seen.add(session.id);
    const descendants = children.get(session.id) ?? [];
    if (visible) rows.push({ session, depth, hasChildren: descendants.some((child) => !seen.has(child.id)) });
    for (const child of descendants) visit(child, depth + 1, visible && !collapsed.has(session.id));
  };
  for (const session of children.get("") ?? []) visit(session, 0, true);
  for (const session of visible) if (!seen.has(session.id)) visit(session, 0, true);
  return rows;
}

export function currentMainSession(sessions: ActivitySession[]) {
  const mains = sessions.filter((session) => !session.parent_session_id)
    .sort((a, b) => b.label.localeCompare(a.label, undefined, { numeric: true }));
  return mains.find((session) => session.status === "running")
    ?? [...sessions].reverse().find((session) => session.status === "running")
    ?? mains.find((session) => isActive(session.status)) ?? mains[0] ?? sessions[0];
}

export const sessionLabel = (session: ActivitySession) => session.session_number ? `#${session.session_number}` : session.label.split(".").join(" / ");

function useResource<T>(path: string | null, refresh: number) {
  const source = useContext(ActivitySource);
  const [result, setResult] = useState<{ path: string; data: T } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const reload = useRef<(() => void) | null>(null);
  useEffect(() => {
    if (!path) return;
    let stopped = false;
    let active = false;
    let pending = false;
    const controller = new AbortController();
    const load = (explicit = true) => {
      // Notifications coalesce while a slow read finishes; only navigation cancels it.
      if (active) { pending = true; return; }
      active = true;
      setLoading(true);
      setError("");
      source.request<T>(path, { signal: controller.signal, cache: explicit ? "reload" : "default" }).then((data) => {
        if (!stopped) { setResult({ path, data }); setError(""); }
      }).catch((e) => {
        if (!stopped && !controller.signal.aborted) setError(e instanceof Error ? e.message : "Unable to load activity.");
      }).finally(() => {
        active = false;
        if (!stopped) {
          setLoading(false);
          if (pending) { pending = false; load(); }
        }
      });
    };
    reload.current = load;
    load(false);
    return () => { stopped = true; reload.current = null; controller.abort(); };
  }, [path, source.request]);
  const previousRefresh = useRef([refresh, source.refreshVersion]);
  useEffect(() => {
    if (previousRefresh.current[0] !== refresh || previousRefresh.current[1] !== source.refreshVersion)
      reload.current?.();
    previousRefresh.current = [refresh, source.refreshVersion];
  }, [refresh, source.refreshVersion]);
  return { data: result?.path === path ? result.data : path ? source.cached?.<T>(path) ?? null : null, loading, error };
}

function Status({ value }: { value: string }) {
  const kind = /fail|error/.test(value) ? "failed" : /succeed|complete|done/.test(value) ? "succeeded"
    : /running/.test(value) ? "running" : /queue|wait|block/.test(value) ? "queued" : "quiet";
  const Icon = kind === "succeeded" ? Check : kind === "failed" ? CircleAlert : kind === "running" ? LoaderCircle : Circle;
  return <span className={`activity-status ${kind}`}><Icon size={12} />{value.replace(/_/g, " ")}</span>;
}

function SubagentDescription({ child, projectId }: {
  child: NonNullable<ActivitySession["subagents"]>[number]; projectId: string;
}) {
  const [open, setOpen] = useState(false);
  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary><span>{child.title.replace(/[-_]/g, " ")}</span><Status value={child.status} /></summary>
    {open && <>
      {child.descriptor_revision != null && <small>Reviewer description revision {child.descriptor_revision}</small>}
      {child.description ? <Suspense fallback={<span>Loading description...</span>}>
        <ActivityMarkdown document={child.description} projectId={projectId} showMetadata={false} />
      </Suspense> : <p>Description was not recorded.</p>}
    </>}
  </details>;
}

function ToolButton({ label, onClick, children, disabled = false }: {
  label: string; onClick: () => void; children: ReactNode; disabled?: boolean;
}) {
  return <button type="button" className="platform-icon-button" title={label} aria-label={label} onClick={onClick} disabled={disabled}>{children}</button>;
}

function ErrorMessage({ children }: { children: string }) {
  return children ? <div className="platform-error" role="alert">{children}</div> : null;
}

function Metrics({ item, run = false }: { item: ActivityRun | ActivitySession; run?: boolean }) {
  const metric = (key: "tokens_in" | "tokens_out" | "cost_usd", label: string) => {
    const estimated = key === "cost_usd" && item.usage?.cost_usd == null && item.usage?.estimated_cost_usd != null;
    const value = estimated ? item.usage.estimated_cost_usd : item.usage?.[key] ?? item.usage?.recorded?.[key];
    const partial = value != null && item.usage?.[key] == null;
    const coverage = estimated ? item.usage.estimated_cost_coverage : item.usage?.coverage?.[key];
    return <div title={estimated ? "Standard API rates as of 2026-09-10; excludes cache-write, service-tier and long-context adjustments. Not billed spend." : undefined}><dt>{estimated ? "Est. API cost" : label}</dt><dd className={value == null ? "unknown" : ""}>{key === "cost_usd" ? cost(value) : tokens(value)}</dd>
      {partial && coverage != null && coverage < (item.usage.attempt_count || 0) && <small>{coverage} / {item.usage.attempt_count} executions reported</small>}
      {estimated && <small>Standard rates</small>}</div>;
  };
  return <>
  <dl className="activity-metrics">
    <div><dt>Elapsed</dt><dd>{activityDuration(item.elapsed_seconds)}</dd></div>
    <div><dt>Agent time</dt><dd>{activityDuration(item.agent_seconds)}</dd></div>
    {metric("cost_usd", "Cost")}
    {metric("tokens_in", "Input tokens")}
    {metric("tokens_out", "Output tokens")}
  </dl>
  {item.usage?.incomplete && <small>Usage is incomplete; reconciliation required.</small>}
  {run && <dl className="activity-session-counts">
    {run && <div><dt>Main sessions</dt><dd>{(item as ActivityRun).main_session_count ?? (item as ActivityRun).session_count}</dd></div>}
    {run && <div><dt>Delegated sessions</dt><dd>{(item as ActivityRun).delegated_session_count ?? 0}</dd></div>}
    {run && <div><dt>Native subagents</dt><dd>{(item as ActivityRun).metrics_pending ? "..." : (item as ActivityRun).native_subagent_count ?? 0}</dd></div>}
    {run && <div><dt>Total sessions</dt><dd>{(item as ActivityRun).total_session_count ?? 0}</dd></div>}
  </dl>}
  </>;
}

function eventCategory(event: ActivityEvent) {
  const kind = event.kind;
  if (/fail|error|cancel|stale|request_changes/.test(kind) || ["failed", "interrupted"].includes(String(event.data?.transfer_state ?? "")) || ["failed", "timed_out"].includes(String(event.data?.status ?? "")) || typeof event.data?.returncode === "number" && event.data.returncode !== 0) return { key: kind, label: "Attention", Icon: CircleAlert, separate: true };
  if (kind === "agent.message") return { key: kind, label: "Agent messages", Icon: MessagesSquare, separate: true };
  if (kind.startsWith("session.goal.") || kind === "session.goal_continuation") return { key: "goals", label: "Goals", Icon: Target, separate: true };
  if ((["session.delegated", "session.completed"].includes(kind) && event.data?.native !== true) ||
      (["subagent.started", "subagent.finished"].includes(kind) && (event.data?.native === false || event.data?.lifecycle === "delegated_session"))) {
    return { key: kind, label: "Delegated sessions", Icon: GitBranch, separate: true };
  }
  if (["session.delegated", "session.completed", "subagent.started", "subagent.finished"].includes(kind)) return { key: kind, label: "Subagents", Icon: GitBranch, separate: true };
  if (/delegat|child/.test(kind)) return { key: "delegation", label: "Delegation", Icon: GitBranch };
  if (/skill/.test(kind)) return { key: "skills", label: "Skills loaded", Icon: Layers };
  const searchTools = ["lean_search", "lean_leansearch", "lean_loogle", "lean_leanfinder", "lean_state_search", "lean_local_search", "lean_hammer_premise"];
  if (kind.startsWith("search.") || (kind === "mcp.tool.called" && searchTools.includes(String(event.data?.tool || "")))) {
    return { key: "search", label: "Search", Icon: Search };
  }
  if (kind.startsWith("mcp.")) return { key: "mcp", label: "MCP calls", Icon: Network };
  if (kind.startsWith("plugin.")) return { key: "plugins", label: "Plugins", Icon: Layers };
  if (kind === "hook.delivered") return { key: kind, label: "Notifications", Icon: Bell, separate: true };
  if (kind.startsWith("hook.") || kind === "report.stop_gate") return { key: kind, label: "Hooks", Icon: Webhook, separate: true };
  if (/report/.test(kind)) return { key: "reports", label: "Reports", Icon: FileText };
  if (kind.startsWith("zulip.")) return kind.includes("read")
    ? { key: "discussions", label: "Discussions read", Icon: MessagesSquare }
    : { key: "messages", label: "Messages posted", Icon: MessagesSquare };
  if (/workspace|checkpoint/.test(kind) || event.data?.repository_kind === "workspace") return { key: "workspace", label: "Workspace", Icon: GitCommitHorizontal };
  if (/pull|review|merge|\.pr\.|forge\.change/.test(kind)) return { key: "pulls", label: "Pull requests", Icon: GitPullRequest };
  if (/proof|proposal|check|validation/.test(kind)) return { key: "checks", label: "Formalization checks", Icon: Check };
  if (kind.startsWith("graph.node.")) return { key: "nodes", label: "Graph nodes", Icon: Network };
  if (kind.startsWith("graph.objective.")) return { key: "objectives", label: "Objectives", Icon: Network };
  if (kind.startsWith("graph.mission.")) return { key: "missions", label: "Missions", Icon: Layers };
  if (kind.startsWith("graph.route.")) return { key: "routes", label: "Proof routes", Icon: Network };
  if (kind.startsWith("graph.graph.")) return { key: "graph", label: "Graph reads", Icon: Network };
  if (kind.startsWith("forge.git.")) return { key: "git", label: "Repository transfers", Icon: GitCommitHorizontal };
  return { key: kind, label: kind.replace(/[._]/g, " "), Icon: Activity };
}

export function groupActivityEvents(events: ActivityEvent[]) {
  const groups: { id: string; category: ReturnType<typeof eventCategory>; events: ActivityEvent[] }[] = [];
  for (const event of events) {
    const category = eventCategory(event);
    const previous = groups[groups.length - 1];
    const first = previous?.events[0];
    if (!category.separate && previous?.category.key === category.key && first?.session_id === event.session_id && first?.attempt_id === event.attempt_id) {
      previous.events.push(event);
    } else {
      groups.push({ id: event.id, category, events: [event] });
    }
  }
  return groups.map((group) => ({ ...group, id: group.events[group.events.length - 1].id }));
}

function EventTime({ event }: { event: ActivityEvent }) {
  return <time dateTime={event.created_at} title={new Date(event.created_at).toLocaleString()}>{timestamp(event.created_at, true)}</time>;
}

function GitEventContent({ event, count = 1 }: { event: ActivityEvent; count?: number }) {
  const data = event.data || {};
  const checkpoint = event.kind === "workspace.checkpoint";
  const link = event.links?.find(link => checkpoint ? /\/commit\//.test(link.url) : Boolean(activityLink(link.url)));
  const href = link && activityLink(link.url);
  const commit = typeof data.commit === "string" ? data.commit : "";
  const repository = [data.organization, data.repository].filter(Boolean).join("/");
  const label = checkpoint ? "Git push" : `Git ${data.operation || event.kind.split(".").pop()}`;
  const reference = checkpoint ? commit.slice(0, 12) || "Commit" : repository || link?.label || "Repository";
  const details = activityEventDetails(event).filter(detail => !["Commit", "Branch"].includes(detail.label));
  const heading = <div className="activity-event-heading activity-git-heading"><span className="activity-git-description">
    <strong>{label}:</strong>{href ? <a href={href} title={checkpoint ? `${commit}${data.branch ? ` (${data.branch})` : ""}` : reference} onClick={event => event.stopPropagation()}>{reference}</a> : <span>{reference}</span>}
    {checkpoint ? <span>{event.title}</span> : <span className="activity-git-state">{count > 1 ? `${count} transfers` : `transfer ${data.transfer_state || "recorded"}`}</span>}
  </span><EventTime event={event} /></div>;
  return <div className="activity-event-content activity-git-event">{details.length ? <details className="activity-git-details"><summary><ChevronRight size={12} className="activity-git-arrow" />{heading}</summary>
    <dl className="activity-event-details">{details.map(detail => <div key={detail.label}><dt>{detail.label}</dt><dd>{detail.code ? <code>{detail.value}</code> : detail.value}</dd></div>)}</dl>
  </details> : heading}</div>;
}

function NotificationDetails({ event }: { event: ActivityEvent }) {
  const notices = activityNotices(event);
  const summary = activityNoticeSummary(event);
  const omitted = typeof event.data?.notices_omitted === "number" ? event.data.notices_omitted : 0;
  return <>
    {summary && <p className="activity-notification-summary">{summary}</p>}
    {!!notices.length && <ul className="activity-notices">{notices.map((notice, index) => {
      const Icon = notice.mentioned ? AtSign : notice.kind.startsWith("zulip.") ? MessagesSquare
        : notice.kind === "mission.dispatched" ? GitBranch : notice.kind.endsWith("succeeded") ? Check
        : notice.kind.endsWith("failed") ? CircleAlert : notice.kind.endsWith("cancelled") ? X : Bell;
      const href = activityLink(notice.url);
      return <li key={index}>
        <Icon size={14} aria-hidden="true" />
        <div><span className="activity-notice-kind">{notice.label}{notice.stream && <span> / {notice.stream}</span>}</span>
          {href ? <a href={href} target="_blank" rel="noreferrer">{notice.title}<ArrowUpRight size={11} /></a> : <span className="activity-notice-title">{notice.title}</span>}
        </div>
      </li>;
    })}</ul>}
    {omitted > 0 && <p>{omitted} more notification{omitted === 1 ? "" : "s"}; titles omitted from this record.</p>}
    {!notices.length && typeof event.data?.summary === "string" && <p>{event.data.summary}</p>}
  </>;
}

function NotificationEventContent({ event }: { event: ActivityEvent }) {
  return <div className="activity-event-content activity-notification-event">
    <div className="activity-event-heading"><strong>{activityEventTitle(event)}</strong><EventTime event={event} /></div>
    <NotificationDetails event={event} />
  </div>;
}

function AgentMessageEventContent({ event, projectId = "" }: { event: ActivityEvent; projectId?: string }) {
  const markdown = typeof event.data?.markdown === "string" ? event.data.markdown : "";
  return <div className="activity-event-content activity-agent-event">
    <div className="activity-event-heading"><strong>{activityEventTitle(event)}</strong><EventTime event={event} /></div>
    {markdown && <Suspense fallback={<p className="activity-agent-message-fallback">{markdown}</p>}>
      <ActivityMarkdown className="activity-agent-message" document={markdown} projectId={projectId} showMetadata={false} />
    </Suspense>}
    {event.data?.truncated === true && <span className="activity-message-state">Truncated</span>}
  </div>;
}

const hookRecord = (value: unknown): Record<string, unknown> | undefined => value && typeof value === "object" && !Array.isArray(value)
  ? value as Record<string, unknown> : undefined;
const hookText = (value: unknown): string => typeof value === "string" ? value : "";
const hookValue = (value: unknown): string => typeof value === "number" ? value.toLocaleString()
  : typeof value === "string" && value ? value : typeof value === "boolean" ? (value ? "Yes" : "No") : "";


function HookBriefingContent({ event, briefing }: { event: ActivityEvent; briefing: Record<string, unknown> }) {
  const sessions = hookRecord(briefing.sessions) ?? {};
  const parallelism = hookRecord(briefing.parallelism) ?? {};
  const pulls = hookRecord(briefing.pull_requests) ?? {};
  const roadmap = hookRecord(pulls.roadmap) ?? hookRecord(pulls.dag) ?? {};
  const mission = hookRecord(briefing.mission);
  const refill = hookRecord(briefing.refill);
  const activeSessions = Array.isArray(briefing.active_sessions)
    ? briefing.active_sessions.map(hookRecord).filter((item): item is Record<string, unknown> => Boolean(item)) : [];
  const metrics: [string, unknown][] = [
    ["Running", sessions.running], ["Queued", sessions.queued], ["Slots available", parallelism.available],
    ["Capacity", parallelism.max_parallelism], ["Budget left", parallelism.budget_remaining],
    ["Roadmap PRs", roadmap.open], ["Awaiting review", roadmap.awaiting_review ?? pulls.awaiting_review],
    ["Awaiting author", roadmap.awaiting_author ?? pulls.awaiting_author],
  ].filter(([, value]) => Boolean(hookValue(value))) as [string, unknown][];
  const missionId = hookText(mission?.id);
  const project = hookText(briefing.project_id) || event.project_id || hookText(event.data?.project_id);
  const missionHref = project && missionId ? `/?project=${encodeURIComponent(project)}&project_view=missions&mission=${encodeURIComponent(missionId)}` : "";
  const links = [["Open frontier", hookText(briefing.frontier_url)], ["Open maintenance", hookText(briefing.maintenance_url)]]
    .filter((item): item is [string, string] => Boolean(activityLink(item[1])));
  const nodeCount = mission?.node_count ?? (Array.isArray(mission?.nodes) ? mission.nodes.length : undefined);
  const missionDetails: [string, unknown][] = [["Status", mission?.status], ["Revision", mission?.revision],
    ["Nodes", nodeCount], ["Node attempts", mission?.node_attempt_count]];
  return <div className="activity-hook-briefing">
    {hookText(briefing.summary) && <p className="activity-hook-summary">{hookText(briefing.summary)}</p>}
    {!!metrics.length && <dl className="activity-hook-metrics">{metrics.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{hookValue(value)}</dd></div>)}</dl>}
    {mission && <section className="activity-hook-mission">
      <span>Mission</span>
      {missionHref ? <a href={missionHref}>{hookText(mission.title) || missionId}<ArrowUpRight size={12} /></a> : <strong>{hookText(mission.title) || missionId || "Current mission"}</strong>}
      {hookText(mission.content) && <p>{hookText(mission.content)}</p>}
      <dl>{missionDetails.filter(([, value]) => Boolean(hookValue(value))).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{hookValue(value)}</dd></div>)}</dl>
    </section>}
    {!!activeSessions.length && <section className="activity-hook-sessions"><strong>Other active sessions</strong><ul>{activeSessions.map((item, index) => {
      const sessionId = hookText(item.session_id);
      const label = hookText(item.mission_id) || hookText(item.target_id) || hookText(item.node_id) || hookText(item.agent) || sessionId;
      const sessionHref = sessionId ? `/?tab=activity${hookText(item.run_id) ? `&run=${encodeURIComponent(hookText(item.run_id))}` : ""}&session=${encodeURIComponent(sessionId)}` : "";
      return <li key={sessionId || index}><span>{hookText(item.status) || "active"}</span>{sessionHref ? <a href={sessionHref}>{label}</a> : <code>{label}</code>}{item.scope === "project" && <small>Other run</small>}</li>;
    })}</ul>{typeof briefing.active_sessions_total === "number" && briefing.active_sessions_total > activeSessions.length && <small>{briefing.active_sessions_total - activeSessions.length} more active sessions</small>}</section>}
    {refill && <p className="activity-hook-refill"><strong>Queue refill</strong>{hookValue(refill.remaining_target)} of {hookValue(refill.dispatch_target)} admissions remaining{hookText(refill.reason) ? ` (${hookText(refill.reason).replace(/_/g, " ")})` : ""}.</p>}
    {!!links.length && <div className="activity-event-links">{links.map(([label, url]) => <a key={label} href={url}>{label}<ArrowUpRight size={12} /></a>)}</div>}
  </div>;
}

function HookEventContent({ event }: { event: ActivityEvent }) {
  const content = typeof event.data?.additional_context === "string" ? event.data.additional_context : "";
  const briefing = hookRecord(event.data?.briefing);
  const isBriefing = content.startsWith("Horizon session briefing (live coordination state):");
  const hasNotices = activityNotices(event).length > 0 || Boolean(activityNoticeSummary(event));
  const reasons = Array.isArray(event.data?.reasons)
    ? event.data.reasons.filter((reason): reason is string => typeof reason === "string" && Boolean(reason)) : [];
  const reference = capabilityReference(event.kind, event.data ?? {});
  const href = reference && capabilityHref(reference.kind, reference.name, event.session_id);
  return <div className="activity-event-content activity-hook-event">
    <div className="activity-event-heading"><strong>{activityEventTitle(event)}</strong><EventTime event={event} /></div>
    {briefing && <HookBriefingContent event={event} briefing={briefing} />}
    {!briefing && isBriefing && <p>Structured briefing details were not retained for this historical event.</p>}
    {hasNotices && <section className="activity-hook-notifications"><strong>Notifications</strong><NotificationDetails event={event} /></section>}
    {!!reasons.length && <ul>{reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
    {event.data?.context_truncated === true && <span className="activity-message-state">Raw hook output truncated</span>}
    {href && <div className="activity-event-links"><a href={href}>{reference.name}<ArrowUpRight size={12} /></a></div>}
    {content && <details className="activity-hook-raw"><summary><ChevronRight size={13} />Raw hook output</summary><pre>{content}</pre></details>}
  </div>;
}

function EventContent({ event, projectId = "" }: { event: ActivityEvent; projectId?: string }) {
  const [expanded, setExpanded] = useState(false);
  const [retry, setRetry] = useState(0);
  const detail = useResource<ActivityEvent>(expanded && event.detail_path ? event.detail_path : null, retry);
  return <>
    <EventBody event={detail.data ?? event} projectId={projectId} />
    {event.detail_path && !detail.data && <div>
      <ErrorMessage>{detail.error}</ErrorMessage>
      <button type="button" className="platform-small-button" disabled={detail.loading} onClick={() => {setExpanded(true); setRetry(value => value + 1);}}>
        <ChevronDown size={14} />{detail.loading ? "Loading full event..." : detail.error ? "Retry full event" : "Show full event"}
      </button>
    </div>}
  </>;
}

function EventBody({ event, projectId = "" }: { event: ActivityEvent; projectId?: string }) {
  if (event.kind.startsWith("forge.git.") || event.kind === "workspace.checkpoint") return <GitEventContent event={event} />;
  if (event.kind === "hook.delivered") return <NotificationEventContent event={event} />;
  if (event.kind === "hook.output" || event.kind === "report.stop_gate") return <HookEventContent event={event} />;
  if (event.kind === "agent.message") return <AgentMessageEventContent event={event} projectId={projectId} />;
  const reference = capabilityReference(event.kind, event.data ?? {});
  const referenceSession = typeof event.data?.capability_session_id === "string" ? event.data.capability_session_id : event.session_id;
  const nodeReferences = activityNodes(event, projectId);
  const nodeProjects = [...new Set(nodeReferences.map(ref => ref.project))];
  const links = [...(event.links ?? [])].filter(link => !activityNodeLink(link.url));
  if (reference) {
    const url = capabilityHref(reference.kind, reference.name, referenceSession);
    if (!links.some((link) => link.url === url)) links.push({ label: reference.name, url });
  }
  const project = typeof event.data?.project_id === "string" ? event.data.project_id : event.project_id || projectId;
  const graphTarget = event.kind.startsWith("graph.") ? event.kind.split(".")[1] : "";
  const tags: { label: string; value: string; href?: string }[] = [];
  const addTag = (label: string, value: unknown, href?: string) => {
    if (typeof value === "string" && value) tags.push({ label, value, href });
  };
  addTag("Objective", event.data?.objective_id, project ? `?project=${encodeURIComponent(project)}&objective=${encodeURIComponent(String(event.data.objective_id))}` : undefined);
  addTag("Task", event.data?.task_id);
  addTag("Route", event.data?.route_id || (graphTarget === "route" ? event.data?.target_id : undefined));
  const details = activityEventDetails(event);
  const repository = [event.data?.organization, event.data?.repository].filter(value => typeof value === "string" && value).join("/");
  const referenceLinks = links.filter((link, index) => !tags.some(tag => tag.href && link.url.replace(/^\//, "") === tag.href.replace(/^\//, "")) && links.findIndex(item => item.url === link.url) === index);
  return <div className="activity-event-content">
    <div className="activity-event-heading"><strong>{activityEventTitle(event)}</strong><time dateTime={event.created_at} title={new Date(event.created_at).toLocaleString()}>{timestamp(event.created_at, true)}</time></div>
    {nodeProjects.map(project => {
      const refs = nodeReferences.filter(ref => ref.project === project);
      return <Suspense key={project} fallback={<div className="activity-event-links">{refs.map(ref => <a key={ref.id} href={`?project=${encodeURIComponent(project)}&node=${encodeURIComponent(ref.id)}`}>{ref.id}</a>)}</div>}>
        <ActivityMarkdown className="activity-event-nodes" document={refs.map(ref => `[[node:${ref.id}]]`).join(" ")} projectId={project} showMetadata={false} />
      </Suspense>;
    })}
    {typeof event.data?.summary === "string" && event.data.summary && <p>{event.data.summary}</p>}
    {typeof event.data?.detail === "string" && event.data.detail && <details className="activity-hook-raw"><summary><ChevronRight size={13} />Details</summary><pre>{event.data.detail}</pre></details>}
    {!!details.length && <dl className="activity-event-details">{details.map(detail => <div key={detail.label}><dt>{detail.label}</dt><dd>{detail.code ? <code>{detail.value}</code> : detail.value}</dd></div>)}</dl>}
    {!!tags.length && <div className="activity-event-tags">{tags.map((tag) => tag.href
      ? <a key={`${tag.label}-${tag.value}`} className="activity-reference-tag" href={tag.href} data-preview={`${tag.label}: ${tag.value}`} title={`${tag.label}: ${tag.value}`}>{tag.label}<code>{tag.value}</code><ArrowUpRight size={11} /></a>
      : <span key={`${tag.label}-${tag.value}`} className="activity-reference-tag" data-preview={`${tag.label}: ${tag.value}`} title={`${tag.label}: ${tag.value}`}><span>{tag.label}</span><code>{tag.value}</code></span>)}</div>}
    {!!referenceLinks.length && <div className="activity-event-links">{referenceLinks.map((link, index) => {
      const href = activityLink(link.url);
      const label = repository && link.label === "Repository" ? repository : link.label === "Node" && !nodeReferences.length ? "Browse nodes" : link.label || "Open reference";
      return href ? <a key={`${link.url}-${index}`} href={href} target="_blank" rel="noreferrer">{label}<ArrowUpRight size={12} /></a> : null;
    })}</div>}
  </div>;
}

export function EventTimeline({ events, projectId }: { events: ActivityEvent[]; projectId?: string }) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  if (!events.length) return <div className="activity-empty">No events recorded.</div>;
  return <ol className="activity-timeline">
    {groupActivityEvents(events).map(({ id, category, events: items }) => {
      const { Icon } = category;
      const latest = items[0], earliest = items[items.length - 1];
      return <li key={id} id={`event-${id}`}>
        <span className="activity-event-symbol"><Icon size={15} /></span>
        {items.length === 1 ? <EventContent event={latest} projectId={projectId} /> : <details className="activity-event-group" open={expanded.has(id)} onToggle={event => {
          if (event.target !== event.currentTarget) return;
          const open = event.currentTarget.open;
          setExpanded(previous => {
            if (previous.has(id) === open) return previous;
            const next = new Set(previous); if (open) next.add(id); else next.delete(id); return next;
          });
        }}>
          <summary><ChevronRight size={14} className="activity-group-arrow" /><strong>{category.label}</strong><span className="activity-group-count">{items.length} events</span>
            <span className="activity-event-range">{earliest.created_at !== latest.created_at && <><time dateTime={earliest.created_at}>{timestamp(earliest.created_at, true)}</time><span>to</span></>}<time dateTime={latest.created_at}>{timestamp(latest.created_at, true)}</time></span>
          </summary>
          {expanded.has(id) && <ol className="activity-group-events">{gitTransferBursts(items).map(burst => <li key={burst[0].id}>
            {burst.length === 1 ? <EventContent event={burst[0]} projectId={projectId} /> : <details className="activity-transfer-group">
              <summary><ChevronRight size={12} className="activity-git-arrow" /><GitEventContent event={burst[0]} count={burst.length} /></summary>
              {burst.map(event => <GitEventContent key={event.id} event={event} />)}
            </details>}
          </li>)}</ol>}
        </details>}
      </li>;
    })}
  </ol>;
}

function contextDocumentKind(key: string): "mission" | "objective" | "node" {
  return key === "mission_id" ? "mission" : key === "objective_id" ? "objective" : "node";
}

function ContextLinks({ context, projectId }: { context?: Context; projectId?: string }) {
  const entries = ["mission_id", "objective_id", "node_id"].flatMap((key) => {
    const value = context?.[key] || (context?.target_kind === key.replace("_id", "") ? context.target_id : undefined);
    return typeof value === "string" && value ? [[key, value]] : [];
  });
  const shown = new Set(entries.filter(([key]) => key === "node_id").map(([, value]) => value));
  const extraNodes = activityContextNodes(context, projectId).filter((ref) => !shown.has(ref.id));
  if (!entries.length && !extraNodes.length) return null;
  return <div className="activity-context-links">{entries.map(([key, value]) => {
    const label = key.replace("_id", "");
    if (projectId) return <Suspense key={key} fallback={<span>{label}: {value}</span>}>
      <ActivityDocumentReference key={`${projectId}:${value}`} projectId={projectId} id={value} kind={contextDocumentKind(key)} title={typeof context?.[`${label}_title`] === "string" ? String(context[`${label}_title`]) : undefined} />
    </Suspense>;
    return <span key={key} className="activity-reference-tag" data-preview={`${label}: ${value}`} title={`${label}: ${value}`}><span>{label}</span><code>{value}</code></span>;
  })}
    {projectId && extraNodes.map((ref) => <Suspense key={ref.id} fallback={<span>node: {ref.id}</span>}>
      <ActivityDocumentReference projectId={ref.project} id={ref.id} kind="node" />
    </Suspense>)}
  </div>;
}

function SessionView({ sessionId, run, refresh, onMutation }: {
  sessionId: string; run: RunDetail; refresh: number; onMutation: () => void;
}) {
  const { basePath: activityPath, request: platformRequest, writable = true } = useContext(ActivitySource);
  const [view, setView] = useState<"report" | "events" | "attempts" | "logs">("events");
  const { data: detail, error, loading } = useResource<SessionDetail>(`${activityPath}/sessions/${encodeURIComponent(sessionId)}?compact=true`, refresh);
  const data = detail ?? run.sessions.find(session => session.id === sessionId);
  const recent = useResource<EventPage>(view === "events" ? `${activityPath}/sessions/${encodeURIComponent(sessionId)}/events?limit=15&compact=true` : null, refresh);
  const reportPage = useResource<{reports: Report[]}>(view === "report" ? `${activityPath}/sessions/${encodeURIComponent(sessionId)}?view=reports` : null, refresh);
  const logs = useResource<DebugLogPage>(view === "logs" ? `${activityPath}/sessions/${encodeURIComponent(sessionId)}/logs?limit=50` : null, refresh);
  const [revision, setRevision] = useState("");
  const [older, setOlder] = useState<EventPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => requestRef.current?.abort(), []);
  const reports = [...(reportPage.data?.reports ?? [])].sort((a, b) => b.revision - a.revision);
  const selectedReport = reports.find((report) => report.id === revision) ?? reports[0];
  const events = useMemo(() => {
    const map = new Map((older?.events ?? []).map((event) => [event.id, event]));
    for (const event of recent.data?.events ?? []) map.set(event.id, event);
    return [...map.values()].sort((a, b) => b.created_at.localeCompare(a.created_at) || (b.sort_at || "").localeCompare(a.sort_at || "") || String(b.id).localeCompare(String(a.id), undefined, { numeric: true }));
  }, [recent.data?.events, older]);
  const cursor = older ? older.next_before : recent.data?.next_before;
  const loadOlder = async () => {
    if (cursor == null || busy) return;
    setBusy(true);
    requestRef.current = new AbortController();
    try {
      const page = await platformRequest<EventPage>(`${activityPath}/sessions/${encodeURIComponent(sessionId)}/events?limit=15&compact=true&before=${encodeURIComponent(String(cursor))}`, { signal: requestRef.current.signal });
      setOlder((previous) => ({ events: [...(previous?.events ?? []), ...page.events], next_before: page.next_before }));
      setActionError("");
    } catch (e) {
      if (!requestRef.current?.signal.aborted) setActionError(e instanceof Error ? e.message : "Unable to load earlier events.");
    } finally { setBusy(false); }
  };
  const resume = async () => {
    setBusy(true);
    requestRef.current = new AbortController();
    try {
      await platformRequest(`${activityPath}/sessions/${encodeURIComponent(sessionId)}/resume`, { method: "POST", body: "{}", signal: requestRef.current.signal });
      setActionError("");
      onMutation();
    } catch (e) {
      if (!requestRef.current?.signal.aborted) setActionError(e instanceof Error ? e.message : "Unable to resume session.");
    } finally { setBusy(false); }
  };
  const prioritize = async (priority: string) => {
    setBusy(true);
    requestRef.current = new AbortController();
    try {
      await platformRequest(`${activityPath}/runs/${encodeURIComponent(run.id)}/queue`, {
        method: "PATCH", body: JSON.stringify({ session_id: sessionId, priority }),
        signal: requestRef.current.signal,
      });
      setActionError("");
      onMutation();
    } catch (e) {
      if (!requestRef.current?.signal.aborted) setActionError(e instanceof Error ? e.message : "Unable to change queue priority.");
    } finally { setBusy(false); }
  };
  if (!data) return <div className="activity-session"><ErrorMessage>{error}</ErrorMessage>{!error && <div className="activity-empty">{loading ? "Loading session..." : "Session unavailable."}</div>}</div>;
  const delegated = run.sessions.filter((session) => session.parent_session_id === data.id && !session.native).length;
  const nativeWorkers = data.subagents?.length || 0;
  const parent = run.sessions.find(session => session.id === data.parent_session_id);
  const runSession = run.sessions.find((session) => session.id === data.id);
  const admission = sessionAdmission(data);
  return <article className="activity-session" aria-label={`Session ${sessionLabel(data)}`}>
    <ErrorMessage>{error || actionError}</ErrorMessage>
    <header className="activity-session-heading">
      <div><div className="activity-eyebrow"><Bot size={13} /><span>Session {sessionLabel(data)}</span><span>{sessionProfile(data)}</span>
        {parent && <a href={`?tab=activity&run=${encodeURIComponent(run.id)}&session=${encodeURIComponent(parent.id)}`}>Parent {sessionLabel(parent)}<ArrowUpRight size={11} /></a>}
      </div><h2>{data.title}</h2></div>
      <div className="activity-heading-actions"><Status value={sessionStatus(data)} />{writable && (data.can_resume ?? ["failed", "cancelled", "interrupted", "blocked"].includes(data.status)) && <ToolButton label="Resume session" onClick={() => void resume()} disabled={busy}><RotateCcw size={15} /></ToolButton>}</div>
    </header>
    <div className="activity-session-meta"><span title={data.created_at}>Started {timestamp(data.started_at)}</span>{data.finished_at && <span>Finished {timestamp(data.finished_at)}</span>}{delegated > 0 && <span>{delegated} delegated {delegated === 1 ? "session" : "sessions"}</span>}{nativeWorkers > 0 && <span>{nativeWorkers} {nativeWorkers === 1 ? "subagent" : "subagents"}</span>}</div>
    {data.status === "queued" && data.recovery?.state && <p className="activity-attempt-error" role="status">
      {data.recovery.state === "waiting_for_infrastructure" ? "Waiting for infrastructure" : "Recovering"}
      {data.recovery.retry_at && ` · Retry after ${timestamp(new Date(data.recovery.retry_at * 1000).toISOString())}`}
      {data.recovery.reason && <><br />{data.recovery.reason}</>}
    </p>}
    <ContextLinks context={data.context} projectId={run.project_id} />
    {detail ? <Metrics item={detail} /> : <div role="status">{loading ? "Loading session details..." : "Session details unavailable."}</div>}
    {detail && <dl className="activity-configuration">
      <div><dt>Models</dt><dd>{data.models?.length ? data.models.join(", ") : "Not recorded"}</dd></div>
      <div><dt>Effort</dt><dd>{data.efforts?.length ? data.efforts.join(", ") : "Not recorded"}</dd></div>
      <div><dt>Host</dt><dd>{data.host_id || "Not assigned"}</dd></div>
      <div><dt>Profile</dt><dd>{data.profile_id || "Not recorded"}</dd></div>
      {data.status === "queued" && !data.native && <div><dt>Queue</dt><dd><select aria-label="Queue position" disabled={busy || !writable}
        value="current" onChange={(event) => void prioritize(event.target.value)}>
        {[["current", "Current position"], ["first", "Move to first"], ["up", "Move up one"], ["down", "Move down one"], ["last", "Move to last"]].map(([value, label]) => <option key={value} value={value}>{label}</option>)}
      </select>{runSession?.queue_position && <span>
        Position {runSession.queue_position}
      </span>}</dd></div>}
      {data.status === "queued" && <div className="activity-admission"><dt>Start conditions</dt><dd>
        <AdmissionDetails admission={admission} />
        {typeof data.context.effort === "string" && <span>Effort: {data.context.effort}</span>}
        {typeof data.context.harness === "string" && <span>Harness: {data.context.harness}</span>}
      </dd></div>}
      {typeof data.context?.agent === "string" && <div><dt>Specialty</dt><dd><a href={capabilityHref("agents", data.context.agent, data.id)} target="_blank" rel="noreferrer">{data.context.agent}</a></dd></div>}
      <div><dt>Skills</dt><dd>{data.skills?.length ? data.skills.map((skill) => <a key={skill} className="activity-skill" href={capabilityHref("skills", skill, data.id)} target="_blank" rel="noreferrer">{skill}</a>) : "None recorded"}</dd></div>
      <div className="activity-subagents"><dt>Subagents</dt><dd>{data.subagents?.length ? data.subagents.map(child =>
        <SubagentDescription key={child.id} child={child} projectId={run.project_id} />) : "None recorded"}</dd></div>
    </dl>}
    <div className="activity-session-tabs" role="tablist" aria-label="Session content">
      {([{ id: "events", title: "Events", Icon: Activity, count: null }, { id: "report", title: "Reports", Icon: FileText, count: reportPage.data ? reports.length : null }, { id: "logs", title: "Logs", Icon: ScrollText, count: null }, { id: "attempts", title: "Executions", Icon: RotateCcw, count: detail?.attempt_count ?? null }] as const).map(({ id, title, Icon, count }) => <button key={id} type="button" id={`activity-tab-${id}`} role="tab" aria-selected={view === id} aria-controls={`activity-panel-${id}`} onClick={() => setView(id)}><Icon size={14} />{title}{count !== null && <span>{count}</span>}</button>)}
    </div>
    <div className="activity-session-panel" id={`activity-panel-${view}`} role="tabpanel" aria-labelledby={`activity-tab-${view}`}>
      <ErrorMessage>{view === "report" ? reportPage.error : view === "events" ? recent.error : ""}</ErrorMessage>
      {(error || (view === "report" && reportPage.error) || (view === "events" && recent.error)) && <ToolButton label="Retry session data" onClick={onMutation}><RefreshCw size={15} /></ToolButton>}
      {view === "report" && (selectedReport ? <>
        <div className="activity-report-toolbar"><label><span>Revision</span><select aria-label="Report revision" value={selectedReport.id} onChange={(event) => setRevision(event.target.value)}>{reports.map((report) => <option key={report.id} value={report.id}>{report.label || `Revision ${report.revision} - ${report.kind}`}</option>)}</select></label><time dateTime={selectedReport.created_at}>{timestamp(selectedReport.created_at)}</time></div>
        <Suspense fallback={<div className="activity-empty">Loading report...</div>}><ActivityMarkdown document={selectedReport.markdown} projectId={run.project_id} repository={workspaceRepository(data.context?.workspace) || workspaceRepository(run.context?.workspace)} showMetadata={false} className="activity-report" /></Suspense>
      </> : <div className="activity-empty"><FileText size={23} /><span>{reportPage.loading ? "Loading report..." : reportPage.error ? "Report unavailable." : "No report published yet."}</span></div>)}
      {view === "events" && <>{!recent.data && !events.length ? <div className="activity-empty">{recent.error ? "Events unavailable." : "Loading events..."}</div> : <EventTimeline events={events} projectId={run.project_id} />}{cursor != null && <button type="button" className="platform-small-button activity-more" disabled={busy} onClick={() => void loadOlder()}>{busy ? "Loading..." : "Earlier events"}<ChevronDown size={14} /></button>}</>}
      {view === "attempts" && (detail?.attempts?.length ? <><p className="activity-panel-note">Each execution is one process run for this session. A later execution retries the same session; it is not a delegated session.</p><ol className="activity-attempts">{[...detail.attempts].sort((a, b) => b.number - a.number).map((attempt) => <li key={attempt.id}>
        <div className="activity-attempt-heading"><strong>Execution {attempt.number}</strong><Status value={attempt.status} /></div>
        <dl><div><dt>Started</dt><dd>{timestamp(attempt.started_at)}</dd></div><div><dt>Finished</dt><dd>{attempt.finished_at ? timestamp(attempt.finished_at) : "Pending"}</dd></div><div><dt>Host</dt><dd>{attempt.host_id || "Not assigned"}</dd></div><div><dt>Profile</dt><dd>{attempt.profile_id || "Not recorded"}</dd></div><div><dt>Model</dt><dd>{attempt.model || "Profile default"}</dd></div><div><dt>Effort</dt><dd>{attempt.effort || "Not recorded"}</dd></div></dl>
        {attempt.error && <p className="activity-attempt-error">{attempt.error}</p>}
      </li>)}</ol></> : <div className="activity-empty">{loading ? "Loading executions..." : error ? "Executions unavailable." : "No executions recorded."}</div>)}
      {view === "logs" && <><ErrorMessage>{logs.error}</ErrorMessage>{logs.data?.records.length ? <ol className="activity-debug-logs">{logs.data.records.map((record, index) => <li key={`${String(record.at ?? index)}-${index}`}><time dateTime={typeof record.at === "string" ? record.at : undefined}>{typeof record.at === "string" ? timestamp(record.at, true) : "Unknown time"}</time><pre>{JSON.stringify(record, null, 2)}</pre></li>)}</ol> : <div className="activity-empty"><ScrollText size={23} /><span>{logs.loading ? "Loading logs..." : "No debug logs recorded."}</span></div>}</>}
    </div>
  </article>;
}

function RunView({ runId, sessionId, refresh, onSelectSession, onBack, onMutation }: {
  runId: string; sessionId: string; refresh: number; onSelectSession: (id: string, replace?: boolean) => void;
  onBack: () => void; onMutation: () => void;
}) {
  const { basePath: activityPath, request: platformRequest, writable = true } = useContext(ActivitySource);
  const { data: summary, error, loading } = useResource<RunDetail>(`${activityPath}/runs/${encodeURIComponent(runId)}?compact=true`, refresh);
  const metrics = useResource<ActivityRun>(summary ? `${activityPath}/runs/${encodeURIComponent(runId)}?view=metrics` : null, refresh);
  const queue = useResource<{admissions: Record<string, Admission>}>(summary ? `${activityPath}/runs/${encodeURIComponent(runId)}?view=queue` : null, refresh);
  const [olderSessions, setOlderSessions] = useState<ActivitySession[]>([]);
  const [olderSessionCursor, setOlderSessionCursor] = useState<string | null>(null);
  const [olderSessionsLoading, setOlderSessionsLoading] = useState(false);
  const [olderSessionsError, setOlderSessionsError] = useState("");
  const sessionsRequestRef = useRef<AbortController | null>(null);
  useEffect(() => {
    sessionsRequestRef.current?.abort();
    setOlderSessions([]);
    setOlderSessionCursor(null);
    setOlderSessionsError("");
  }, [runId, refresh]);
  useEffect(() => {
    if (!olderSessions.length) setOlderSessionCursor(summary?.sessions_next_before ?? null);
  }, [summary?.sessions_next_before, olderSessions.length]);
  const loadedSessions = useMemo(() => {
    const values = new Map<string, ActivitySession>();
    for (const session of summary?.sessions ?? []) values.set(session.id, session);
    for (const session of olderSessions) values.set(session.id, session);
    return [...values.values()];
  }, [summary?.sessions, olderSessions]);
  const data = useMemo(() => summary ? {...summary, ...(metrics.data ? {
    usage: metrics.data.usage, agent_seconds: metrics.data.agent_seconds, models: metrics.data.models,
    efforts: metrics.data.efforts, native_subagent_count: metrics.data.native_subagent_count,
    subagent_count: metrics.data.subagent_count, metrics_pending: false,
  } : {}), sessions: loadedSessions.map(session => ({...session,
    context: {...session.context, admission: queue.data?.admissions?.[session.id] ?? session.context?.admission}}))} : null,
    [summary, loadedSessions, metrics.data, queue.data]);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [showAllSessions, setShowAllSessions] = useState(false);
  const [treeFilter, setTreeFilter] = useState<"all" | "active">("all");
  const [busy, setBusy] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [actionError, setActionError] = useState("");
  const requestRef = useRef<AbortController | null>(null);
  useEffect(() => () => { requestRef.current?.abort(); sessionsRequestRef.current?.abort(); }, []);
  useEffect(() => {
    const now = Date.now();
    const deadlines = queuedSessions(data?.sessions ?? []).flatMap(session => {
      const admission = sessionAdmission(session);
      return [admission.next_check_at, admission.expires_at];
    }).filter((value): value is string => !!value).map(value => new Date(value).getTime()).filter(value => value > now);
    if (!deadlines.length) return;
    const timer = window.setTimeout(onMutation, Math.min(2_147_000_000, Math.min(...deadlines) - now + 25));
    return () => window.clearTimeout(timer);
  }, [data, onMutation]);
  useEffect(() => {
    if (data?.sessions.length && !sessionId) {
      const current = currentMainSession(data.sessions);
      if (current?.id) onSelectSession(current.id, true);
    }
  }, [data, sessionId]);
  useEffect(() => {
    if (!data || !sessionId) return;
    const parents = new Set<string>();
    let current = data.sessions.find((session) => session.id === sessionId);
    while (current?.parent_session_id && !parents.has(current.parent_session_id)) {
      parents.add(current.parent_session_id);
      current = data.sessions.find((session) => session.id === current?.parent_session_id);
    }
    setCollapsed((previous) => {
      if (![...parents].some((id) => previous.has(id))) return previous;
      return new Set([...previous].filter((id) => !parents.has(id)));
    });
  }, [sessionId]);
  const counts = sessionCounts((data?.sessions ?? []).filter(session => !session.native));
  const runningCount = data?.running_session_count ?? counts.running;
  const queuedCount = data?.queued_session_count ?? counts.queued;
  const slots = (data?.slot_capacity != null && data.slot_capacity > 0 ? data.slot_capacity : runSlotCapacity(data?.execution));
  const occupancyLabel = slots != null ? `${runningCount}/${slots} running` : `${runningCount} running`;
  const occupancyTitle = slots != null ? `${runningCount} of ${slots} scheduler slots running` : `${runningCount} sessions running`;
  const runningSessions = [...(data?.sessions ?? [])].filter((session) => session.status === "running")
    .sort((left, right) => (left.session_number ?? 0) - (right.session_number ?? 0) || left.label.localeCompare(right.label, undefined, { numeric: true }));
  const pendingSessions = queuedSessions(data?.sessions ?? []);
  const rows = sessionTree(data?.sessions ?? [], collapsed, treeFilter);
  const selectedRowIndex = rows.findIndex(({ session }) => session.id === sessionId);
  const visibleRows = showAllSessions ? rows : rows.slice(0, 5);
  useEffect(() => {
    setShowAllSessions(Boolean(sessionId) && selectedRowIndex >= 5);
  }, [sessionId, selectedRowIndex, treeFilter]);
  const nextSessionCursor = olderSessionCursor ?? summary?.sessions_next_before ?? null;
  const loadOlderSessions = async () => {
    if (!nextSessionCursor || olderSessionsLoading) return;
    sessionsRequestRef.current?.abort();
    const controller = new AbortController();
    sessionsRequestRef.current = controller;
    setOlderSessionsLoading(true);
    setOlderSessionsError("");
    try {
      const page = await platformRequest<RunDetail>(`${activityPath}/runs/${encodeURIComponent(runId)}?compact=true&sessions_before=${encodeURIComponent(nextSessionCursor)}`, { signal: controller.signal });
      if (!controller.signal.aborted) {
        setOlderSessions((previous) => [...previous, ...(page.sessions || [])]);
        setOlderSessionCursor(page.sessions_next_before ?? null);
      }
    } catch (e) {
      if (!controller.signal.aborted) setOlderSessionsError(e instanceof Error ? e.message : "Unable to load older sessions.");
    } finally {
      if (!controller.signal.aborted) setOlderSessionsLoading(false);
    }
  };
  const cancel = async () => {
    setBusy(true);
    requestRef.current = new AbortController();
    try {
      await platformRequest(`${activityPath}/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST", body: "{}", signal: requestRef.current.signal });
      setConfirmCancel(false);
      setActionError("");
      onMutation();
    } catch (e) {
      if (!requestRef.current?.signal.aborted) setActionError(e instanceof Error ? e.message : "Unable to cancel run.");
    } finally { setBusy(false); }
  };
  return <>
    <div className="activity-run-navigation"><button className="activity-back" type="button" onClick={onBack}><ArrowLeft size={15} />All runs</button><ToolButton label="Refresh activity" onClick={onMutation} disabled={loading}><RefreshCw size={15} /></ToolButton></div>
    <ErrorMessage>{error || actionError}</ErrorMessage>
    {!data ? !error && <div className="activity-empty">Loading run...</div> : <>
      <header className="activity-run-heading"><div><div className="activity-eyebrow">Run #{data.number}{data.project_id && <a href={`?project=${encodeURIComponent(data.project_id)}`}>{data.project_title || data.project_id}<ArrowUpRight size={12} /></a>}</div><h2>{data.title || `Run #${data.number}`}</h2><RunPhase phase={data.phase} /></div><div className="activity-heading-actions"><Status value={data.status} />{writable && isActive(data.status) && <ToolButton label="Cancel run" onClick={() => setConfirmCancel(true)} disabled={busy}><Square size={14} /></ToolButton>}</div></header>
      {confirmCancel && <div className="activity-cancel-confirm" role="alert"><span>Cancel this run and its active sessions?</span><button type="button" className="platform-small-button" disabled={busy} onClick={() => void cancel()}><Square size={13} />{busy ? "Cancelling..." : "Cancel run"}</button><ToolButton label="Keep running" onClick={() => setConfirmCancel(false)}><X size={15} /></ToolButton></div>}
      <ContextLinks context={data.context} projectId={data.project_id} />
      <Metrics item={data} run />
      {metrics.loading && !metrics.data && <small role="status">Loading usage...</small>}
      <ErrorMessage>{metrics.error || queue.error}</ErrorMessage>
      <div className="activity-current-sessions" aria-label="Session occupancy">
        <span className="activity-occupancy running" title={occupancyTitle}><LoaderCircle size={13} />{occupancyLabel}</span>
        {runningSessions.map((session) => <button key={session.id} type="button" className="activity-occupancy-session" aria-current={session.id === sessionId ? "true" : undefined} title={session.title} onClick={() => onSelectSession(session.id)}>{sessionLabel(session)}</button>)}
        {runningCount === 0 && queuedCount === 0 && <span>None active</span>}
      </div>
      {!!pendingSessions.length && <div className="activity-queue" aria-label="Queued sessions">
        <span className="activity-queue-label"><Clock3 size={13} />Queue</span>
        <ol>{pendingSessions.map((session) => <li key={session.id}>
          <button type="button" className="activity-queue-session" aria-current={session.id === sessionId ? "true" : undefined}
            aria-describedby={`queue-condition-${session.id}`} onClick={() => onSelectSession(session.id)}>
            <span className="activity-queue-position">{session.queue_position}</span>
            <strong>{sessionLabel(session)}</strong><span className="activity-queue-title">{session.title}</span>
            <span className={`activity-queue-state ${sessionAdmission(session).state || "unknown"}`} aria-hidden="true" />
          </button>
          <span className="activity-queue-tooltip" role="tooltip" id={`queue-condition-${session.id}`}>
            <strong>{sessionLabel(session)}: {session.title}</strong>
            <AdmissionDetails admission={sessionAdmission(session)} />
          </span>
        </li>)}</ol>
      </div>}
      <div className="activity-run-meta"><time dateTime={data.created_at}>{timestamp(data.created_at)}</time>{data.models?.length > 0 && <span>{data.models.join(", ")}</span>}{data.stop_reason && <span>{data.status === "active" ? data.stop_reason : `Stopped: ${data.stop_reason.replace(/_/g, " ")}`}</span>}</div>
      <div className="activity-workspace">
        <aside className="activity-session-sidebar" aria-label="Agent tree">
          <div className="activity-sidebar-heading"><h3>Agent tree</h3><span>{data.sessions.length}{data.session_count > data.sessions.length ? ` / ${data.session_count}` : ""}{treeFilter === "active" ? " active" : " loaded"}</span></div>
          <label className="activity-tree-filter"><input type="checkbox" checked={treeFilter === "active"} onChange={event => setTreeFilter(event.target.checked ? "active" : "all")} />Running or queued</label>
          <ul className={`activity-session-tree ${showAllSessions ? "expanded" : "limited"}`}>{visibleRows.map(({ session, depth, hasChildren }) => <li key={session.id} style={{ "--session-depth": Math.min(depth, 6) } as CSSProperties}>
            <div className={`activity-tree-row ${session.id === sessionId ? "selected" : ""}`}>
              {hasChildren ? <button type="button" className="activity-tree-toggle" aria-label={`${collapsed.has(session.id) ? "Expand" : "Collapse"} session ${session.label}`} aria-expanded={!collapsed.has(session.id)} onClick={() => setCollapsed((previous) => {
                const next = new Set(previous); if (next.has(session.id)) next.delete(session.id); else next.add(session.id); return next;
              })}>{collapsed.has(session.id) ? <ChevronRight size={13} /> : <ChevronDown size={13} />}</button> : <span className="activity-tree-toggle" />}
              <button type="button" className={`activity-session-select ${session.native ? "native-subagent" : session.parent_session_id ? "delegated-session" : "main-session"}`} aria-current={session.id === sessionId ? "true" : undefined} onClick={() => onSelectSession(session.id)}><span className="activity-tree-label"><span>{sessionLabel(session)}</span><span>{sessionProfile(session)}</span></span><strong>{session.title}</strong><Status value={sessionStatus(session)} /></button>
            </div>
          </li>)}</ul>
          {!rows.length && <div className="activity-empty">{treeFilter === "active" ? "No running or queued sessions." : "No sessions."}</div>}
          {rows.length > 5 && <button type="button" className="platform-small-button activity-session-more" onClick={() => setShowAllSessions((value) => !value)}>{showAllSessions ? "Show less" : `Show more (${rows.length - 5})`}</button>}
          {olderSessionsError && <ErrorMessage>{olderSessionsError}</ErrorMessage>}
          {nextSessionCursor && <button type="button" className="platform-small-button activity-session-more" disabled={olderSessionsLoading} onClick={() => void loadOlderSessions()}>{olderSessionsLoading ? "Loading older sessions..." : "Load older sessions"}<ChevronDown size={14} /></button>}
        </aside>
        {sessionId ? <SessionView key={sessionId} sessionId={sessionId} run={data} refresh={refresh} onMutation={onMutation} /> : <div className="activity-empty">No session selected.</div>}
      </div>
    </>}
  </>;
}

function ActivityBody({ projects = [] }: { projects?: Project[] }) {
  const { basePath: activityPath, request: platformRequest, onNavigate, location: selection } = useContext(ActivitySource);
  const [runId, setRunId] = useState(() => new URLSearchParams(location.search).get("run") ?? "");
  const [sessionId, setSessionId] = useState(() => new URLSearchParams(location.search).get("session") ?? "");
  const [project, setProject] = useState("");
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [older, setOlder] = useState<RunPage | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const olderRequest = useRef<AbortController | null>(null);
  useEffect(() => {
    if (selection) { setRunId(selection.runId); setSessionId(selection.sessionId); }
  }, [selection?.runId, selection?.sessionId]);
  const params = new URLSearchParams({ limit: "25", compact: "true" });
  if (project) params.set("project_id", project);
  if (status) params.set("status", status);
  const path = `${activityPath}/runs?${params}`;
  const { data, error, loading } = useResource<RunPage>(runId || sessionId ? null : path, refresh);
  const directoryMetrics = useResource<RunPage>(!runId && !sessionId && data ? path.replace("compact=true", "compact=false") : null, refresh);
  const sessionLink = useResource<SessionDetail>(sessionId && !runId ? `${activityPath}/sessions/${encodeURIComponent(sessionId)}?compact=true` : null, refresh);
  useEffect(() => {
    const navigate = () => {
      const params = new URLSearchParams(location.search);
      setRunId(params.get("run") ?? ""); setSessionId(params.get("session") ?? "");
      if (params.has("view")) {
        const url = new URL(location.href); url.searchParams.delete("view"); history.replaceState({}, "", url);
      }
    };
    navigate();
    window.addEventListener("popstate", navigate);
    return () => { window.removeEventListener("popstate", navigate); olderRequest.current?.abort(); };
  }, []);
  useEffect(() => { olderRequest.current?.abort(); setOlder(null); setActionError(""); setBusy(false); }, [path]);
  const navigate = (run: string, session = "", replace = false) => {
    const url = dashboardUrl("activity");
    url.searchParams.delete("view");
    if (run) url.searchParams.set("run", run); else url.searchParams.delete("run");
    if (session) url.searchParams.set("session", session); else url.searchParams.delete("session");
    if (!onNavigate) history[replace ? "replaceState" : "pushState"]({}, "", url);
    setRunId(run); setSessionId(session);
    onNavigate?.({ tab: "activity", run, session, assignment: "" }, replace);
  };
  useEffect(() => {
    if (!runId && sessionLink.data?.id === sessionId && sessionLink.data.run_id) navigate(sessionLink.data.run_id, sessionId, true);
  }, [runId, sessionId, sessionLink.data]);
  const runs = useMemo(() => {
    const items = new Map((older?.runs ?? []).map((run) => [run.id, run]));
    for (const run of data?.runs ?? []) items.set(run.id, run);
    for (const run of directoryMetrics.data?.runs ?? []) if (items.has(run.id)) items.set(run.id, {...items.get(run.id)!, usage: run.usage});
    const needle = query.trim().toLocaleLowerCase();
    return [...items.values()].sort((a, b) => b.number - a.number).filter((run) => !needle || [run.title, run.id, String(run.number), run.project_id].some((value) => value?.toLocaleLowerCase().includes(needle)));
  }, [data, directoryMetrics.data, older, query]);
  const cursor = older ? older.next_before : data?.next_before;
  const loadOlder = async () => {
    if (cursor == null || busy) return;
    setBusy(true);
    olderRequest.current = new AbortController();
    const controller = olderRequest.current;
    try {
      const page = await platformRequest<RunPage>(`${path}&before=${encodeURIComponent(String(cursor))}`, { signal: controller.signal });
      if (!controller.signal.aborted) {
        setOlder((previous) => ({ runs: [...(previous?.runs ?? []), ...page.runs], next_before: page.next_before }));
        setActionError("");
      }
    } catch (e) {
      if (!controller.signal.aborted) setActionError(e instanceof Error ? e.message : "Unable to load earlier runs.");
    } finally { if (!controller.signal.aborted) setBusy(false); }
  };
  return <section className="horizon-activity" aria-label="Run activity">
    {runId ? <RunView key={runId} runId={runId} sessionId={sessionId} refresh={refresh} onSelectSession={(id, replace) => navigate(runId, id, replace)} onBack={() => navigate("")} onMutation={() => setRefresh((value) => value + 1)} /> : sessionId ? <>
      <button className="activity-back" type="button" onClick={() => navigate("")}><ArrowLeft size={15} />All runs</button>
      <ErrorMessage>{sessionLink.error}</ErrorMessage>
      {!sessionLink.error && <div className="activity-empty">Loading session...</div>}
    </> : <>
      <div className="activity-filters"><label className="activity-search"><Search size={15} /><input type="search" aria-label="Search runs" placeholder="Search runs" value={query} onChange={(event) => setQuery(event.target.value)} /></label><select aria-label="Filter by project" value={project} onChange={(event) => setProject(event.target.value)}><option value="">All projects</option>{projects.map((item) => <option key={item.id} value={item.id}>{item.title || item.id}</option>)}</select><select aria-label="Filter by status" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All statuses</option>{["queued", "running", "succeeded", "failed", "cancelled", "blocked"].map((value) => <option key={value} value={value}>{value[0].toUpperCase() + value.slice(1)}</option>)}</select><ToolButton label="Refresh activity" onClick={() => setRefresh((value) => value + 1)} disabled={loading}><RefreshCw size={15} /></ToolButton></div>
      <ErrorMessage>{error || actionError}</ErrorMessage>
      <div className="activity-run-list">
        {!!runs.length && <div className="activity-run-columns" aria-hidden="true"><span>Run</span><span>Status</span><span>Elapsed</span><span>Cost</span><span>Started</span><span /></div>}
        {runs.map((run) => <button type="button" key={run.id} className="activity-run-row" onClick={() => navigate(run.id)}>
          <span className="activity-run-title"><strong><span className="activity-run-number">#{run.number}</span>{run.title || run.id}</strong><span className="activity-run-subtitle"><RunPhase phase={run.phase} />{run.project_title || run.project_id || "No project"}<span>{run.main_session_count ?? run.session_count} main</span>{(run.delegated_session_count ?? 0) > 0 && <span>{run.delegated_session_count} delegated</span>}{(run.native_subagent_count ?? run.subagent_count ?? 0) > 0 && <span>{run.native_subagent_count ?? run.subagent_count} native</span>}</span></span>
          <Status value={run.status} /><span className="activity-run-duration"><Clock3 size={12} />{activityDuration(run.elapsed_seconds)}</span><span className={`activity-run-cost ${run.usage?.cost_usd == null && run.usage?.estimated_cost_usd == null ? "unknown" : ""}`} title={run.usage?.cost_usd == null && run.usage?.estimated_cost_usd != null ? "Estimated API cost at standard rates" : undefined}>{run.usage?.cost_usd == null && run.usage?.estimated_cost_usd != null ? "~" : ""}{cost(run.usage?.cost_usd ?? run.usage?.estimated_cost_usd ?? run.usage?.recorded?.cost_usd)}</span><time dateTime={run.started_at || run.created_at}>{timestamp(run.started_at || run.created_at)}</time><ChevronRight className="activity-run-arrow" size={15} />
        </button>)}
      </div>
      {!runs.length && !error && <div className="activity-empty activity-empty-runs"><Activity size={26} /><span>{loading && !data ? "Loading runs..." : query || project || status ? "No matching runs." : "No runs yet."}</span></div>}
      {cursor != null && <button type="button" className="platform-small-button activity-more" disabled={busy} onClick={() => void loadOlder()}>{busy ? "Loading..." : "Earlier runs"}<ChevronDown size={14} /></button>}
    </>}
  </section>;
}

export default function ActivityTab({ projects = [], dataSource }: { projects?: Project[]; dataSource: ActivityDataSource }) {
  return <ActivitySource.Provider value={dataSource}><ActivityBody projects={projects} /></ActivitySource.Provider>;
}
