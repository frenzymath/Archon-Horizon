import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import ActivityTab, { type ActivityDataSource } from "../components/ActivityTab";
import { request, type Command, type Project } from "./api";

type Session = {id: string; revision: number; status: string; native: boolean; queue_position?: number};
export type ActivityRevision = {id: string; revision: number; sessions?: Session[]};

export function activityCommand(path: string, init: RequestInit, records: Map<string, ActivityRevision>, writable: boolean): Command | null {
  if (!writable) throw new Error("This account cannot change activity");
  const action = path.match(/^\/dashboard\/activity\/(runs|sessions)\/([^/]+)\/(cancel|resume|queue)$/);
  if (!action || (action[3] === "resume") !== (action[1] === "sessions")) throw new Error("Unsupported activity command");
  let target = decodeURIComponent(action[2]);
  let operation: string;
  const args: Record<string, unknown> = {};
  if (action[3] === "queue") {
    const body = JSON.parse(String(init.body || "{}")) as {session_id: string; priority: string};
    const run = records.get(target);
    target = body.session_id;
    const queued = (run?.sessions || []).filter(session => !session.native && session.status === "queued")
      .sort((a, b) => (a.queue_position || 0) - (b.queue_position || 0));
    const position = queued.findIndex(session => session.id === target);
    if (position < 0) throw new Error("Refresh activity before changing this queue");
    if (!["current", "first", "last", "up", "down"].includes(body.priority)) throw new Error("Unsupported queue position");
    if (body.priority === "current") return null;
    const neighbor = body.priority === "first" ? queued[0] : body.priority === "last" ? queued[queued.length - 1]
      : body.priority === "up" ? queued[position - 1] : queued[position + 1];
    if (!neighbor || neighbor.id === target) return null;
    operation = ["first", "up"].includes(body.priority) ? "move_before" : "move_after";
    args.other_id = neighbor.id;
  } else operation = action[3] === "cancel" ? "cancel_run" : "retry_assignment";
  const record = records.get(target);
  if (!record) throw new Error("Refresh activity before changing this record");
  return {operation, target_id: target, expected_revision: record.revision, args};
}

export type DesktopActivityProps = {
  accountId: string;
  projects: Project[];
  projectId?: string;
  runId: string;
  sessionId: string;
  writable: boolean;
  command: (value: Command) => Promise<boolean>;
  onNavigate: (values: Record<string, string>, replace?: boolean) => void;
};

export default function DesktopActivity({accountId, projects, runId, sessionId, writable, command, onNavigate}: DesktopActivityProps) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const client = useQueryClient();
  const records = useRef(new Map<string, ActivityRevision>());
  const commands = useRef(command);
  commands.current = command;
  const canWrite = useRef(writable);
  canWrite.current = writable;
  useEffect(() => { records.current.clear(); }, [accountId]);
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const visible = (key: readonly unknown[]) => key[0] === "pipeline" && key[1] === accountId &&
      key[2] === "assignments" && typeof key[4] === "string" &&
      (runId ? key[4].split("?")[0] === `/dashboard/activity/runs/${encodeURIComponent(runId)}` ||
        Boolean(sessionId && (key[4].split("?")[0] === `/dashboard/activity/sessions/${encodeURIComponent(sessionId)}` ||
          key[4].startsWith(`/dashboard/activity/sessions/${encodeURIComponent(sessionId)}/`)))
        : key[4].startsWith("/dashboard/activity/runs?"));
    const unsubscribe = client.getQueryCache().subscribe(event => {
      if (event.type !== "updated" || event.action.type !== "invalidate" || !visible(event.query.queryKey) || timer) return;
      timer = setTimeout(() => {
        timer = undefined;
        // A manual refresh or completed command may already have fetched the invalidated data.
        if (!document.hidden && client.getQueryCache().findAll().some(query => visible(query.queryKey) && query.state.isInvalidated))
          setRefreshVersion(value => value + 1);
      }, 1000);
    });
    return () => { unsubscribe(); clearTimeout(timer); };
  }, [client, accountId, runId, sessionId]);
  const transport = useCallback(async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
    if (!path.startsWith("/dashboard/activity/")) throw new Error("Unsupported activity resource");
    if (!init.method || init.method === "GET") {
      const queryKey = ["pipeline", accountId, "assignments", "global", path];
      const cancel = () => { void client.cancelQueries({queryKey, exact: true}); };
      if (init.signal?.aborted) throw new DOMException("Aborted", "AbortError");
      init.signal?.addEventListener("abort", cancel, {once: true});
      let response: T;
      try {
        response = await client.fetchQuery({queryKey, staleTime: init.cache === "reload" ? 0 : 15_000,
          retry: false, queryFn: ({signal}) => request<T>(path, {...init, signal})});
      } finally { init.signal?.removeEventListener("abort", cancel); }
      const record = response as Partial<ActivityRevision>;
      if (record.id && typeof record.revision === "number") records.current.set(record.id,
        {...records.current.get(record.id), ...record} as ActivityRevision);
      record.sessions?.forEach(session => records.current.set(session.id, session));
      return response;
    }
    const value = activityCommand(path, init, records.current, canWrite.current);
    if (!value) return {} as T;
    const completed = await commands.current(value);
    if (!completed) throw new Error("The command is pending or needs attention");
    return {} as T;
  }, [accountId, client]);
  const cached = useCallback(<T,>(path: string) => client.getQueryData<T>(["pipeline", accountId, "assignments", "global", path]), [accountId, client]);
  const source = useMemo<ActivityDataSource>(() => ({basePath: "/dashboard/activity", request: transport,
    cached, writable, refreshVersion,
    location: {runId, sessionId}, onNavigate}), [transport, cached, writable, refreshVersion, runId, sessionId, onNavigate]);
  return <ActivityTab key={accountId} projects={projects} dataSource={source} />;
}
