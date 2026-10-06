import { Play } from "lucide-react";
import ActivityDocumentReference from "./ActivityDocumentReference";
import "./sessionActivityTags.css";

type Item = Record<string, any>;

export const activityHref = (runId: string, sessionId = "") =>
  `/?tab=activity&run=${encodeURIComponent(runId)}${sessionId ? `&session=${encodeURIComponent(sessionId)}` : ""}`;

function sessionText(item: Item): string {
  const number = item.session_number ?? item.sessionNumber ?? item.session_label ?? item.sessionLabel;
  if (typeof number === "number" && Number.isFinite(number)) return `#${number}`;
  const label = String(number || "").trim();
  if (/^\d+(?:\.\d+)*$/.test(label)) return `#${label}`;
  if (label && !/^session[_-]/i.test(label)) return label.startsWith("#") ? label : `#${label}`;
  return "session";
}

function uniqueSessions(items: Item[]): Item[] {
  const seen = new Set<string>();
  const result: Item[] = [];
  for (const item of items) {
    const session = String(item.session_id || item.sessionId || "").trim();
    const run = String(item.run_id || item.runId || item.root_run_id || "").trim();
    if (!session || !run) continue;
    const key = `${run}:${session}`;
    if (seen.has(key)) continue;
    seen.add(key);
    result.push({ ...item, session_id: session, run_id: run });
  }
  return result;
}

export function SessionActivityTag({
  runId, sessionId, label, title, kind = "history",
}: {
  runId: string; sessionId?: string; label: string; title?: string; kind?: "live" | "history";
}) {
  if (!runId) return null;
  return <a className={`platform-session-tag${kind === "live" ? " live" : ""}`} href={activityHref(runId, sessionId)} title={title || label}>
    {kind === "live" && <Play size={11} />}
    {label}
  </a>;
}

export function LiveSessionTags({ activities, projectId = "" }: { activities: Item[]; projectId?: string }) {
  const sessions = uniqueSessions(activities);
  if (!sessions.length) return null;
  return <>{sessions.map((item) => {
    const harness = item.harness || "Agent";
    const host = item.host_id || "execution host";
    const missionId = String(item.mission_id || "").trim();
    const project = String(item.project_id || projectId || "").trim();
    const session = sessionText(item);
    const running = session === "session" ? "running" : `${session} running`;
    if (missionId && project) {
      return <span key={`${item.run_id}:${item.session_id}`} className="platform-session-live">
        <ActivityDocumentReference projectId={project} id={missionId} kind="mission" />
        <SessionActivityTag kind="live" runId={item.run_id} sessionId={item.session_id}
          label={running} title={`${harness} on ${host}`} />
      </span>;
    }
    return <SessionActivityTag key={`${item.run_id}:${item.session_id}`} kind="live" runId={item.run_id} sessionId={item.session_id}
      label={session === "session" ? "Running session" : running}
      title={`${harness} on ${host}`} />;
  })}</>;
}

export function HistorySessionTag({
  sessionId, runId, label, title,
}: {
  sessionId?: string; runId?: string; label: string; title?: string;
}) {
  if (!sessionId || !runId) return null;
  return <SessionActivityTag runId={runId} sessionId={sessionId} label={label} title={title} />;
}

export function RecordSessionTags({ item, createdLabel = "Created", updatedLabel = "Updated" }: {
  item?: Item | null; createdLabel?: string; updatedLabel?: string;
}) {
  if (!item) return null;
  const createdSession = String(item.created_session_id || "").trim();
  const updatedSession = String(item.updated_session_id || "").trim();
  const createdRun = String(item.created_run_id || "").trim();
  const updatedRun = String(item.updated_run_id || "").trim();
  const same = Boolean(createdSession && createdRun && createdSession === updatedSession && createdRun === updatedRun);
  return <>
    <HistorySessionTag sessionId={createdSession} runId={createdRun} label={same ? `${createdLabel} in session` : createdLabel}
      title={same ? `${createdLabel} and ${updatedLabel.toLowerCase()} by this session` : `${createdLabel} by this session`} />
    {!same && <HistorySessionTag sessionId={updatedSession} runId={updatedRun} label={updatedLabel} title={`${updatedLabel} by this session`} />}
  </>;
}

export function NodeSessionTags({ node, activities = [], projectId = "" }: { node?: Item | null; activities?: Item[]; projectId?: string }) {
  const live = uniqueSessions(activities);
  const created = Boolean(node?.created_session_id && node?.created_run_id);
  const updated = Boolean(node?.updated_session_id && node?.updated_run_id);
  if (!live.length && !created && !updated) return null;
  return <div className="platform-session-tags" aria-label="Session links">
    <LiveSessionTags activities={activities} projectId={projectId || String(node?.project_id || "")} />
    <RecordSessionTags item={node} />
  </div>;
}
