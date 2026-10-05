import type { ActivityEvent } from "../components/ActivityTab";

export type ActivityNode = { project: string; id: string };
const text = (value: unknown): string => typeof value === "string" ? value : "";
const strings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];

const noticeTypes: Record<string, [string, string, string]> = {
  "mission.dispatched": ["Mission dispatched", "mission dispatch", "mission dispatches"],
  "mission.finished.succeeded": ["Session succeeded", "successful session", "successful sessions"],
  "mission.finished.failed": ["Session failed", "failed session", "failed sessions"],
  "mission.finished.cancelled": ["Session cancelled", "cancelled session", "cancelled sessions"],
  "zulip.message": ["New Zulip message", "Zulip message", "Zulip messages"],
  "zulip.mention": ["Mentioned in Zulip", "Zulip mention", "Zulip mentions"],
  "zulip.private": ["Zulip direct message", "Zulip direct message", "Zulip direct messages"],
  "zulip.private.mention": ["Mentioned in a Zulip direct message", "Zulip direct mention", "Zulip direct mentions"],
};

function noticeType(kind: string, mentioned: boolean) {
  const key = mentioned && kind === "zulip.message" ? "zulip.mention" : mentioned && kind === "zulip.private" ? "zulip.private.mention" : kind;
  const fallback = kind.replace(/[._]/g, " ") || "Notification";
  return noticeTypes[key] || [fallback, `${fallback} notice`, `${fallback} notices`];
}

const records = (value: unknown): Record<string, unknown>[] => Array.isArray(value)
  ? value.filter((item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item)) : [];

export function activityNotices(event: ActivityEvent) {
  return records(event.data?.notices).map(notice => {
    const kind = text(notice.kind), mentioned = notice.priority === 1;
    return { kind, mentioned, label: noticeType(kind, mentioned)[0], title: text(notice.title) || "Untitled notice",
      url: text(notice.url), stream: text(notice.stream) };
  });
}

export function activityNoticeSummary(event: ActivityEvent): string {
  const counts = records(event.data?.notice_counts).filter(item => typeof item.count === "number" && item.count > 0);
  if (counts.length) return counts.map(item => {
    const [, singular, plural] = noticeType(text(item.kind), item.priority === 1);
    return `${item.count} ${item.count === 1 ? singular : plural}`;
  }).join(", ");
  const notices = activityNotices(event);
  if (notices.length) {
    const groups = new Map<string, { kind: string; mentioned: boolean; count: number }>();
    for (const notice of notices) {
      const previous = groups.get(notice.label);
      groups.set(notice.label, { ...notice, count: (previous?.count || 0) + 1 });
    }
    return [...groups.values()].map(item => {
      const [, singular, plural] = noticeType(item.kind, item.mentioned);
      return `${item.count} ${item.count === 1 ? singular : plural}`;
    }).join(", ");
  }
  return "";
}

function graphReference(url: string, field: "node"): ActivityNode | undefined {
  try {
    const parsed = new URL(url, "https://horizon.invalid");
    if (!url.startsWith("?") && !url.startsWith("/")) {
      if (typeof location === "undefined" || parsed.origin !== location.origin) return;
    }
    if (!["/", "/pipeline"].includes(parsed.pathname) || !["http:", "https:"].includes(parsed.protocol) || url.startsWith("//")) return;
    const project = parsed.searchParams.get("project"), id = parsed.searchParams.get(field);
    return project && id ? { project, id } : undefined;
  } catch { return; }
}

export const activityNodeLink = (url: string) => graphReference(url, "node");

// Coalesce adjacent protocol exchanges, without implying they are distinct Git commands.
export function gitTransferBursts(events: ActivityEvent[]): ActivityEvent[][] {
  const bursts: ActivityEvent[][] = [];
  for (const event of events) {
    const previous = bursts[bursts.length - 1];
    const first = previous?.[0];
    const completed = (item: ActivityEvent) => item.kind.startsWith("forge.git.") && item.data?.transfer_state === "completed" && !(Number(item.data.http_status) >= 400);
    if (first && completed(first) && completed(event) && first.kind === event.kind && first.session_id === event.session_id && first.attempt_id === event.attempt_id
      && ["project_id", "organization", "repository"].every(key => first.data[key] === event.data[key])
      && JSON.stringify(first.links) === JSON.stringify(event.links)
      && Math.abs(Date.parse(first.created_at) - Date.parse(event.created_at)) <= 5000) previous.push(event);
    else bursts.push([event]);
  }
  return bursts;
}

export function activityContextNodes(context?: Record<string, unknown>, projectId = ""): ActivityNode[] {
  const project = text(context?.project_id) || projectId;
  const ids = [text(context?.node_id), context?.target_kind === "node" ? text(context?.target_id) : "", ...strings(context?.node_ids)];
  const seen = new Set<string>();
  return ids.flatMap(id => {
    if (!project || !id || !/^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(id) || seen.has(id)) return [];
    seen.add(id);
    return [{ project, id }];
  });
}

export function activityNodes(event: ActivityEvent, projectId = ""): ActivityNode[] {
  const data = event.data || {};
  const linked = (event.links || []).flatMap(link => activityNodeLink(link.url) || []);
  const project = text(data.project_id) || event.project_id || projectId || linked[0]?.project || "";
  const id = text(data.node_id) || (event.kind.startsWith("graph.node.") ? text(data.target_id) : "");
  const references = [...linked, ...[id, text(data.dependency_of), ...strings(data.node_ids)].filter(Boolean).map(id => ({ project, id }))];
  const seen = new Set<string>();
  return references.filter(ref => {
    const key = JSON.stringify([ref.project, ref.id]);
    if (!ref.project || !/^[A-Za-z0-9][A-Za-z0-9_.:-]*$/.test(ref.id) || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function checkLabel(event: ActivityEvent): string {
  const command = strings(event.data?.command);
  if (command[0]?.split("/").pop() === "lake") {
    if (command[1] === "env" && command[2]?.split("/").pop() === "lean") return command.includes("--server") ? "Lean LSP" : "Lean file check";
    if (command.includes("build")) return "Lake build";
    if (command[1] === "env") return "Lake environment command";
  }
  if (event.data?.mode === "lsp") return "Lean LSP";
  if (event.data?.mode === "lean") return "Lean file check";
  if (event.data?.mode === "lake") return "Lake build";
  return "Lean check";
}

export function activityEventTitle(event: ActivityEvent): string {
  const data = event.data || {};
  if (event.kind === "agent.message") return text(data.external_id) ? `${text(data.agent) || event.title || "Subagent"} said` : "Agent said";
  if (event.kind.startsWith("lean.check.")) {
    const phase = event.kind.slice("lean.check.".length);
    const label = checkLabel(event);
    if (phase === "started") return `${label} started`;
    if (phase === "building") return `${label} running`;
    if (phase === "finished") return `${label} ${data.status === "timed_out" ? "timed out" : data.returncode === 0 || data.ok === true ? "passed" : "failed"}`;
  }
  if (event.kind.startsWith("forge.git.")) {
    const operation = text(data.operation) || event.kind.split(".").pop();
    return `Git ${operation} transfer ${text(data.transfer_state) || "recorded"}`;
  }
  if (event.kind === "graph.node.read" && !activityNodes(event).length) {
    if (data.search) return `Searched nodes: ${data.search}`;
    if (Array.isArray(data.requested_node_ids)) return "Read node summaries";
    return "Node list read";
  }
  if (event.kind.startsWith("graph.node.") && activityNodes(event).length && /^Node (read|created|updated)(:|$)/.test(event.title)) return `Node ${event.kind.split(".").pop()}`;
  if (event.kind === "mcp.tool.called" && data.tool) {
    const tool = [data.server, data.tool].filter(Boolean).join(" / ");
    return data.query ? `MCP search: ${tool} — ${data.query}` : `MCP call: ${tool}`;
  }
  if (event.kind === "search.workspace") return `Searched workspace${data.mode ? ` (${data.mode})` : ""}: ${text(data.query) || event.title}`;
  if (event.kind === "search.library") return `Searched library ${text(data.name) || "pool"}${data.mode ? ` (${data.mode})` : ""}: ${text(data.query) || event.title}`;
  if (event.kind === "search.library.added") return `Added search library: ${text(data.name) || event.title}`;
  if (event.kind === "search.library.removed") return `Removed search library: ${text(data.name) || event.title}`;
  if (event.kind === "search.library.listed") return "Listed search libraries";
  if (event.kind === "session.delegated") return `${data.native === true ? "Native subagent launched" : data.native === false ? "Delegated session queued" : data.external_id ? "Delegated session launched" : "Delegated session queued"}: ${text(data.title) || event.title}`;
  if (event.kind === "subagent.started") return `${data.native === true ? "Native subagent launched" : data.native === false || data.lifecycle === "delegated_session" ? "Delegated session started" : "Subagent launched"}: ${text(data.title) || event.title}`;
  if (event.kind === "session.completed" || event.kind === "subagent.finished") return `${data.native === true ? "Native subagent finished" : data.native === false ? "Delegated session finished" : "Subagent finished"}: ${text(data.title) || event.title}`;
  if (event.kind === "hook.delivered") {
    const count = typeof data.count === "number" ? data.count : activityNotices(event).length;
    return count ? `${count} notification${count === 1 ? "" : "s"} delivered` : "Notifications delivered";
  }
  if (event.kind === "hook.output") return `${text(data.hook) || "Harness"} hook supplied context`;
  return event.title || event.kind.replace(/[._]/g, " ");
}

export function activityEventDetails(event: ActivityEvent): { label: string; value: string; code?: boolean }[] {
  const data = event.data || {};
  const details: { label: string; value: string; code?: boolean }[] = [];
  const add = (label: string, value: unknown, code = false) => {
    if ((typeof value === "string" && value) || typeof value === "number") details.push({ label, value: String(value), code });
  };
  const command = strings(data.command);
  if (command.length) add("Command", command.map(arg => /^[A-Za-z0-9_./:@=+-]+$/.test(arg) ? arg : `'${arg.replace(/'/g, "'\\''")}'`).join(" "), true);
  else if (strings(data.targets).length) add("Targets", strings(data.targets).join(", "), true);
  add("Directory", data.cwd, true);
  if (strings(data.operations).length) add("Operations", strings(data.operations).join(" > "));
  if (strings(data.files).length) add("Files", strings(data.files).join("\n"), true);
  add("Branch", data.branch, true);
  add("Commit", data.commit || data.source_revision, true);
  if (typeof data.returncode === "number") add("Exit code", data.returncode);
  if (typeof data.duration_seconds === "number") add("Duration", `${data.duration_seconds.toLocaleString()} s`);
  if (data.cache) add("Cache", ({ local: "Local", restored: "Restored from shared cache", published: "Built and published", built: "Built locally", offline: "Local build; shared cache unavailable", upload_failed: "Build passed; cache upload failed" } as Record<string, string>)[String(data.cache)] || data.cache);
  if (event.kind.startsWith("graph.node.")) {
    if (typeof data.returned_count === "number") add("Results", `${data.returned_count}${typeof data.total === "number" ? ` of ${data.total}` : ""} nodes`);
    if (typeof data.revision === "number") add("Revision", data.revision);
    if (data.status) add("Filter", data.status);
    if (!data.target_id && !data.node_id && !Array.isArray(data.node_ids) && !activityNodes(event).length) add("References", "Node identities were not recorded");
  }
  if (event.kind.startsWith("graph.mission.")) {
    add("Status", data.status);
    add("Revision", data.revision);
  }
  if (event.kind.startsWith("search.") || (event.kind === "mcp.tool.called" && data.query)) {
    add("Scope", data.scope);
    add("Mode", data.mode);
    add("Query", data.query, true);
    add("Library", data.library || data.lib || data.name, true);
    if (typeof data.hit_count === "number") add("Hits", data.hit_count);
    if (typeof data.declaration_count === "number") add("Indexed", data.declaration_count);
  }
  if (["session.delegated", "session.completed", "subagent.started", "subagent.finished"].includes(event.kind)) {
    add("Description", data.description);
    add("Status", data.status);
    if (typeof data.native === "boolean") add("Kind", data.native ? "Native in-session subagent" : "Delegated Horizon session");
  }
  if (event.kind.startsWith("forge.git.") && Number(data.http_status) >= 400) add("HTTP status", data.http_status);
  if (event.kind === "hook.delivered") {
    add("Types", activityNoticeSummary(event));
  }
  add("Reason", data.archive_reason);
  add("Error", data.error);
  return details;
}
