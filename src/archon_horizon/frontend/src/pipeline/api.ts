export type Page<T> = { items: T[]; next_cursor: string | null };
export type Project = {
  id: string;
  number: number;
  title: string;
  slug: string;
  description?: string;
  workflow?: "legacy" | "milestones";
  revision?: number;
};
export type Run = {
  id: string;
  number: number;
  project_id: string;
  mission_id: string;
  title?: string;
  phase: { kind: string };
  status: string;
  revision: number;
};
export type Assignment = {
  id: string;
  number: number;
  run_id: string;
  run_number: number;
  title: string;
  role: string;
  functions: string[];
  status: string;
  revision: number;
  why_waiting: string | null;
  not_before: string | null;
  expires_at: string | null;
  start_condition?: unknown;
  last_activity: string | null;
  last_activity_at?: string | null;
  host_name: string | null;
  updated_at: string;
};
export type Obligation = {
  id: string;
  number: number;
  description: string;
  status: string;
  resolution?: unknown;
};
export type Execution = {
  id: string;
  number: number;
  status: string;
  host_id: string;
  started_at: string | null;
  finished_at: string | null;
  failure?: unknown;
};
export type ProviderThread = {
  id: string;
  kind?: string;
  parent_id?: string | null;
  reviewer_descriptor_id?: string | null;
  status: string;
  provider_thread_id?: string | null;
  model?: string;
  harness?: string;
  sandbox?: string;
};
export type Activity = {
  id: string;
  kind: string;
  summary: string | null;
  occurred_at: string;
  artifact_url?: string | null;
  category?: string;
  title?: string;
  detail?: string | null;
  source?: string;
  usage?: {input_tokens: number | null; cached_input_tokens: number | null; output_tokens: number | null; cost_usd: number | null} | null;
};
export type AssignmentDetail = Assignment & {
  mission: { title: string; objective: string };
  obligations: Obligation[];
  executions: Execution[];
  provider_threads: ProviderThread[];
  activity: Activity[];
  publications: Change[];
  baseline?: string | null;
};
export type RoadmapNode = {
  id: string;
  number: number;
  title: string;
  status: string;
  source_path?: string;
  source_url?: string;
  dependencies: string[];
  labels?: string[];
  description?: string;
  content?: string;
  source_key?: string;
  dependency_numbers?: number[];
};
export type Roadmap = Page<RoadmapNode> & {
  baseline?: { id: string; status: string; source_commit_oid: string };
  document?: string;
  documents?: {id: string; title: string; content: string; source_url?: string}[];
  summary?: {node_count: number; dependency_count: number; indexed_at: string | null};
};
export type Change = {
  id: string;
  title: string;
  repository: string;
  status: string;
  kind: string;
  revision: number;
  commit_oid?: string;
  url?: string;
  failure?: string | null;
  reviewed_head?: string | null;
  review_status?: string | null;
  updated_at: string;
  worker_managed?: boolean;
};
export type ChangesPage = Page<Change> & {
  preservation?: {
    pending_count: number;
    blocked_count: number;
    oldest_pending_at: string | null;
    last_verified_at: string | null;
  };
};
export type Discussion = {
  id: string;
  title: string;
  url?: string;
  subscribed: boolean;
  unread_count: number;
  revision: number;
  updated_at: string;
  messages?: {
    id: string;
    author: string;
    content: string;
    created_at: string;
  }[];
};
export type Host = {
  id: string;
  name: string;
  status: string;
  slots: number;
  occupied_slots: number;
  heartbeat_at: string | null;
  detail?: string;
};
export type Storage = {
  category: string;
  bytes: number;
  protected_bytes: number;
  reclaimable_bytes: number;
};
export type Resources = {
  hosts: Host[];
  storage: Storage[];
  observed_at: string | null;
  free_bytes: number | null;
  oldest_pending_delivery: string | null;
  backup_status: string;
  provider_status: string;
  limits?: {
    id: string;
    slug: string;
    kind: string;
    max_concurrent: number;
    occupied: number;
    cooldown_until: string | null;
  }[];
};
export type Settings = {
  revision: number;
  configuration: Record<string, unknown>;
  restart_required?: string[];
};
export type Operation = {
  id: string;
  status: "pending" | "running" | "completed" | "failed" | "uncertain";
  result?: unknown;
  error?: string;
};
export type Command = {
  operation: string;
  target_id: string;
  expected_revision?: number;
  args?: Record<string, unknown>;
};
export type PipelineEvent = {
  sequence: number;
  project_id?: string;
  run_id?: string;
  resources: string[];
  gap?: boolean;
};
export type Account = {
  id: string;
  username: string;
  role: string;
  max_offline_replay_seconds?: number;
  permissions?: { write: boolean; admin: boolean };
};

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const read = !init.method || init.method.toUpperCase() === "GET";
  for (let attempt = 0; ; attempt++) {
    try {
      return await requestOnce<T>(path, init, read ? 30000 : 15000);
    } catch (error) {
      const transient = error instanceof TypeError || error instanceof ApiError &&
        [408, 429, 500, 502, 503, 504].includes(error.status);
      if (!read || !transient || init.signal?.aborted || attempt >= 2) throw error;
      await new Promise<void>((resolve, reject) => {
        const abort = () => { window.clearTimeout(timer); reject(init.signal?.reason); };
        const timer = window.setTimeout(() => {
          init.signal?.removeEventListener("abort", abort);
          resolve();
        }, 1000 * 2 ** attempt * (0.8 + Math.random() * 0.4));
        init.signal?.addEventListener("abort", abort, {once: true});
        if (init.signal?.aborted) abort();
      });
    }
  }
}

async function requestOnce<T>(path: string, init: RequestInit, timeoutMs: number): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  const abort = () => controller.abort();
  init.signal?.addEventListener("abort", abort, { once: true });
  if (init.signal?.aborted) controller.abort();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  try {
    const response = await fetch(`/api/v3${path}`, {
      ...init,
      headers,
      signal: controller.signal,
      credentials: "same-origin",
      cache: path.startsWith("/roadmap") ? "no-cache" : "no-store",
    });
    if (!response.ok) {
      const body = (await response.json().catch(() => ({}))) as {
        detail?: string;
        error?: string | { message: string };
      };
      throw new ApiError(
        typeof body.detail === "string"
          ? body.detail
          : typeof body.error === "string"
            ? body.error
            : body.error?.message || `Request failed (${response.status})`,
        response.status,
      );
    }
    return (await response.json()) as T;
  } catch (error) {
    if (timedOut && !init.signal?.aborted)
      throw new ApiError("The connection timed out. Check your connection and retry; previously loaded data is retained.", 408);
    throw error;
  } finally {
    window.clearTimeout(timeout);
    init.signal?.removeEventListener("abort", abort);
  }
}

export function params(
  values: Record<string, string | number | null | undefined>,
) {
  const result = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "")
      result.set(key, String(value));
  });
  return result.toString();
}

export const reference = (
  assignment: Pick<Assignment, "run_number" | "number">,
) => `R${assignment.run_number}/A${assignment.number}`;
