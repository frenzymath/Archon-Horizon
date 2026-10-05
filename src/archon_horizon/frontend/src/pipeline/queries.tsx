import { useCallback, useEffect, useRef, useState } from "react";
import {
  QueryClient,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  ApiError,
  request,
  type Command,
  type Operation,
  type Page,
  type PipelineEvent,
} from "./api";

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30000,
      gcTime: 300000,
      retry: false,
      refetchOnWindowFocus: false,
      refetchOnReconnect: false,
      networkMode: "always",
    },
    mutations: { retry: false, networkMode: "always" },
  },
});

export function useRead<T>(
  accountId: string,
  resource: string,
  scope: string,
  path: string,
  enabled = true,
) {
  return useQuery<T, Error>({
    queryKey: ["pipeline", accountId, resource, scope, path],
    queryFn: ({ signal }) => request<T>(path, { signal }),
    enabled,
  });
}

export function usePagedRead<T>(
  accountId: string,
  resource: string,
  scope: string,
  path: string,
  enabled = true,
) {
  return useInfiniteQuery({
    queryKey: ["pipeline", accountId, resource, scope, path],
    initialPageParam: "",
    enabled,
    queryFn: ({ signal, pageParam }) =>
      request<Page<T>>(
        `${path}${path.includes("?") ? "&" : "?"}cursor=${encodeURIComponent(pageParam)}`,
        { signal },
      ),
    getNextPageParam: (last: Page<T>) => last.next_cursor || undefined,
  });
}

export function useUpdates(accountId: string, projectId: string) {
  const client = useQueryClient();
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    if (!accountId) return;
    let stream: EventSource | null = null;
    let last = 0;
    let closed = false;
    let failures = 0;
    let reconnect: number | undefined;
    let flush: number | undefined;
    const dirty = new Set<string>();
    const refresh = (resources?: string[]) => {
      void client.invalidateQueries({
        predicate: (query) =>
          query.queryKey[0] === "pipeline" &&
          query.queryKey[1] === accountId &&
          (!resources || resources.includes(String(query.queryKey[2]))) &&
          (!projectId ||
            query.queryKey[3] === projectId ||
            query.queryKey[3] === "global"),
      }, {cancelRefetch: false});
    };
    const connect = () => {
      if (closed || document.hidden) return;
      const search = new URLSearchParams({ after: String(last) });
      if (projectId) search.set("project_id", projectId);
      stream = new EventSource(`/api/v3/events?${search}`);
      stream.onopen = () => {
        setConnected(true);
        failures = 0;
        // The server's initial gap establishes the cursor and refreshes snapshots.
        // Refreshing here as well cancels/restarts the same in-flight requests.
      };
      const receive = (message: MessageEvent) => {
        try {
          const event = JSON.parse(message.data) as PipelineEvent;
          if (event.gap) {
            refresh();
            last = event.sequence;
            return;
          }
          if (!Number.isSafeInteger(event.sequence) || event.sequence <= last)
            return;
          last = event.sequence;
          for (const resource of event.resources || []) dirty.add(resource);
          if (flush === undefined)
            flush = window.setTimeout(() => {
              refresh([...dirty]);
              dirty.clear();
              flush = undefined;
            }, 200);
        } catch {
          refresh();
        }
      };
      stream.onmessage = receive;
      stream.addEventListener("invalidate", receive as EventListener);
      stream.addEventListener("gap", (event) => {
        try {
          last =
            (JSON.parse((event as MessageEvent).data) as PipelineEvent)
              .sequence ?? last;
        } catch {
          /* A full refresh recovers malformed gap payloads. */
        }
        refresh();
      });
      stream.onerror = () => {
        setConnected(false);
        stream?.close();
        stream = null;
        const delay =
          Math.min(30000, 1000 * 2 ** Math.min(failures++, 5)) *
          (0.8 + Math.random() * 0.4);
        reconnect = window.setTimeout(() => {
          connect();
        }, delay);
      };
    };
    const visible = () => {
      window.clearTimeout(reconnect);
      stream?.close();
      stream = null;
      setConnected(false);
      if (!document.hidden) connect();
    };
    document.addEventListener("visibilitychange", visible);
    connect();
    return () => {
      closed = true;
      stream?.close();
      window.clearTimeout(reconnect);
      window.clearTimeout(flush);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [accountId, projectId, client]);
  return connected;
}

const pendingStorage = "horizon.pipeline.pending-command";
type PendingCommand = {
  id: string;
  accountId: string;
  projectId: string;
  createdAt: number;
  command: Command;
};

export function clearPendingCommand() {
  sessionStorage.removeItem(pendingStorage);
}

export function useCommand(
  accountId: string,
  projectId: string,
  connected: boolean,
  replaySeconds = 0,
) {
  const client = useQueryClient();
  const [operation, setOperation] = useState<Operation | null>(null);
  const [error, setError] = useState("");
  const [receipt, setReceipt] = useState<PendingCommand | null>(null);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const pending = useRef(false);
  useEffect(() => {
    if (!accountId) return;
    setReceipt(null);
    setOperation(null);
    try {
      const raw = sessionStorage.getItem(pendingStorage);
      if (!raw) return;
      if (raw.length > 16384) throw new Error("Pending command is too large.");
      const saved = JSON.parse(raw) as PendingCommand;
      if (saved.accountId !== accountId) {
        clearPendingCommand();
        return;
      }
      if (
        typeof saved.id !== "string" ||
        typeof saved.projectId !== "string" ||
        !Number.isFinite(saved.createdAt) ||
        typeof saved.command?.operation !== "string" ||
        typeof saved.command?.target_id !== "string"
      )
        throw new Error("Stored command cannot be decoded.");
      setReceipt(saved);
      setOperation({ id: saved.id, status: "uncertain" });
      setError("A previous command is unconfirmed.");
    } catch (failure) {
      setStorageBlocked(true);
      setError(
        `Command recovery storage is unavailable: ${failure instanceof Error ? failure.message : String(failure)}`,
      );
    }
  }, [accountId]);
  const finish = useCallback((result: Operation) => {
    if (["completed", "failed"].includes(result.status)) {
      clearPendingCommand();
      setReceipt(null);
    }
    setOperation(result);
  }, []);
  const reconcile = useCallback(
    async (id: string) => {
      try {
        const result = await request<Operation>(
          `/operations/${encodeURIComponent(id)}`,
        );
        finish(result);
        if (result.status === "completed") {
          setError("");
          void client.invalidateQueries({ queryKey: ["pipeline", accountId] });
        } else if (result.status === "failed")
          setError(result.error || "Command failed.");
      } catch (failure) {
        setError(
          `Command ${id} remains unconfirmed. ${failure instanceof Error ? failure.message : String(failure)}`,
        );
      }
    },
    [accountId, client, finish],
  );
  useEffect(() => {
    if (
      connected &&
      !pending.current &&
      operation &&
      ["uncertain", "pending", "running"].includes(operation.status)
    )
      void reconcile(operation.id);
  }, [connected, operation?.id, operation?.status, reconcile]);
  const submit = async (saved: PendingCommand) => {
    if (
      pending.current ||
      !connected ||
      storageBlocked ||
      saved.accountId !== accountId ||
      saved.projectId !== projectId
    )
      return false;
    pending.current = true;
    const { id, command } = saved;
    setOperation({ id, status: "pending" });
    setError("");
    try {
      const result = await request<Operation>("/commands", {
        method: "POST",
        headers: { "Idempotency-Key": id },
        body: JSON.stringify(command),
      });
      if (result.status === "failed")
        setError(result.error || "Command failed.");
      else
        await client.invalidateQueries({ queryKey: ["pipeline", accountId] });
      finish(result);
      return result.status === "completed";
    } catch (failure) {
      const definite =
        failure instanceof ApiError &&
        failure.status >= 400 &&
        failure.status < 500 &&
        failure.status !== 401 &&
        failure.status !== 403 &&
        failure.status !== 408 &&
        failure.status !== 429;
      finish({ id, status: definite ? "failed" : "uncertain" });
      setError(
        `${definite ? "Command rejected" : "Command unconfirmed"}: ${failure instanceof Error ? failure.message : String(failure)}`,
      );
      return false;
    } finally {
      pending.current = false;
    }
  };
  const execute = async (command: Command) => {
    if (
      pending.current ||
      !connected ||
      storageBlocked ||
      receipt ||
      !accountId ||
      !projectId
    )
      return false;
    const saved: PendingCommand = {
      id: crypto.randomUUID(),
      accountId,
      projectId,
      createdAt: Date.now(),
      command,
    };
    try {
      const raw = JSON.stringify(saved);
      if (raw.length > 16384)
        throw new Error("Command exceeds recovery storage limit.");
      sessionStorage.setItem(pendingStorage, raw);
      setReceipt(saved);
    } catch (failure) {
      setError(
        `Command was not sent: ${failure instanceof Error ? failure.message : String(failure)}`,
      );
      return false;
    }
    return submit(saved);
  };
  const retryAllowed =
    !!receipt &&
    receipt.projectId === projectId &&
    connected &&
    replaySeconds > 0 &&
    Date.now() - receipt.createdAt >= 0 &&
    Date.now() - receipt.createdAt < replaySeconds * 1000;
  return {
    execute,
    operation,
    error,
    reconcile,
    pendingProject: receipt?.projectId,
    retryAllowed,
    retry: async () => {
      if (
        !retryAllowed ||
        !receipt ||
        Date.now() - receipt.createdAt >= replaySeconds * 1000
      )
        return false;
      return submit(receipt);
    },
    busy:
      storageBlocked ||
      !!receipt ||
      (!!operation &&
        ["pending", "running", "uncertain"].includes(operation.status)),
  };
}
