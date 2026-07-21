import type { GitCommit } from './hooks/useGitLog';

// The transcript path string must match the Python endpoint registry exactly
// (it is what gets sha256-hashed in static mode), so ref is interpolated raw.
const transcriptPath = (ref: string) => `/api/transcript?ref=${ref}`;
const reportPath = (ref: string) => `/api/report?ref=${ref}`;

// Conditional-GET cache: remember the ETag + parsed body per URL so a repeat
// fetch can send `If-None-Match` and, when the server answers `304 Not Modified`,
// reuse the cached body instead of re-downloading it. This is what keeps the 5s
// `/api/state` poll from re-transferring several MB every tick when nothing has
// changed. Bounded so a long session browsing many transcripts/files can't grow
// it without limit (the hot polled endpoints stay resident because each 200
// re-inserts them as most-recent).
const ETAG_CACHE_MAX = 96;
const etagCache = new Map<string, { etag: string; data: unknown }>();

async function getJson<T>(url: string): Promise<T> {
  const cached = etagCache.get(url);
  const headers: Record<string, string> = {};
  if (cached) headers['If-None-Match'] = cached.etag;
  // `no-store` keeps the browser's own HTTP cache out of the way so the server
  // alone decides 200-vs-304 based on the ETag we send.
  const res = await fetch(url, { headers, cache: 'no-store' });
  if (res.status === 304 && cached) {
    // Refresh recency so it isn't evicted, then return the retained body.
    etagCache.delete(url);
    etagCache.set(url, cached);
    return cached.data as T;
  }
  const data = await parseJsonResponse<T>(res, url);
  const etag = res.headers.get('ETag');
  if (etag) {
    etagCache.delete(url);
    etagCache.set(url, { etag, data });
    if (etagCache.size > ETAG_CACHE_MAX) {
      const oldest = etagCache.keys().next().value;
      if (oldest !== undefined) etagCache.delete(oldest);
    }
  }
  return data as T;
}

async function parseJsonResponse<T>(res: Response, label: string): Promise<T> {
  const text = await res.text();
  let data: any = null;
  if (text.trim()) {
    try {
      data = JSON.parse(text);
    } catch {
      const preview = text.replace(/\s+/g, ' ').slice(0, 180);
      throw new Error(`${label} returned non-JSON (${res.status} ${res.statusText || 'status'}): ${preview || 'empty response'}`);
    }
  }
  if (!res.ok) {
    throw new Error(data?.error || `${label} -> ${res.status}`);
  }
  return data as T;
}

export const getState = () => getJson<any>('/api/state');
// Light per-project DAGs, split out of /api/state: they change only on
// publish/sync, so the ETag cache turns this poll into a 304 almost always.
export const getBlueprints = () => getJson<Record<string, any>>('/api/blueprints');
export const getTranscripts = () => getJson<any[]>('/api/transcripts');
export const getTranscript = (ref: string) => getJson<any[]>(transcriptPath(ref));
export const getReport = (ref: string) => getJson<{ markdown: string; recommendation?: string }>(reportPath(ref));

export interface BlueprintChaptersResponse {
  chapters: { slug: string; title: string; tex: string }[];
  macros?: Record<string, string>;
  docTitle?: string | null;
  docAuthor?: string | null;
  hasBlueprint: boolean;
  error: string | null;
}
export const getBlueprintChapters = (project: string) =>
  getJson<BlueprintChaptersResponse>(`/api/blueprint/chapters?project=${encodeURIComponent(project)}`);

// Full (heavy) per-project blueprint DAG — node statements, proofs, and Lean
// source — fetched on demand by the Blueprint and DAG pages. Kept out of
// /api/state (which now carries only light DAG nodes) so the 5s poll stays small.
export interface BlueprintDagResponse {
  nodes?: any[];
  edges?: any[];
  [key: string]: any;
}
export const getBlueprintDag = (project: string) =>
  getJson<BlueprintDagResponse>(`/api/blueprint/dag?project=${encodeURIComponent(project)}`);

export interface SourceFile { path: string; size: number; sorries?: number; loc?: number; loc_code?: number }

export interface ProjectStat {
  name: string;
  depends_on?: string[];
  lean_files: number;
  loc: number;
  loc_code: number;
  sorries: number;
  blueprint_nodes: number;
  blueprint_leanok: number;
}
export interface ProjectTrendPoint {
  sha: string;
  short_sha: string;
  date: string;
  subject: string;
  lean_files: number;
  loc: number;
  loc_code: number;
  sorries: number;
}
export const getProjects = () =>
  getJson<{ projects: ProjectStat[]; totals: Omit<ProjectStat, 'name'> }>('/api/projects');
export const getProjectHistory = (project: string, limit = 10) =>
  getJson<{ project: string; limit?: number; history: ProjectTrendPoint[] }>(
    `/api/project/history?project=${encodeURIComponent(project)}&limit=${encodeURIComponent(String(limit))}`,
  );
export const getSourceFiles = (project: string) =>
  getJson<{ files: SourceFile[] }>(`/api/source?project=${encodeURIComponent(project)}`);
export const getSourceFile = (project: string, path: string) =>
  getJson<{ path: string; size: number; content: string }>(
    `/api/source/file?project=${encodeURIComponent(project)}&path=${encodeURIComponent(path)}`,
  );

export const getGitLog = (project: string) =>
  getJson<{ commits: GitCommit[] }>(`/api/git/log?project=${encodeURIComponent(project)}`);

export type ChangeCategory = 'lean' | 'blueprint' | 'other';
export interface SessionChangeFile {
  path: string;
  category: ChangeCategory;
  add?: number;
  del?: number;
  loc_before?: number;
  loc_after?: number;
  loc_code_before?: number;
  loc_code_after?: number;
  sorry_before?: number;
  sorry_after?: number;
  sorry_delta?: number;
  decl_before?: Record<string, number>;
  decl_after?: Record<string, number>;
  decl_delta?: Record<string, number>;
  added?: boolean;
  deleted?: boolean;
}
export interface ChangeRollup {
  files: number;
  add: number;
  del: number;
  loc_after: number;
  loc_code_after: number;
  loc_delta: number;
  loc_code_delta: number;
  sorry_after: number;
  sorry_delta: number;
  decl_after?: Record<string, number>;
  decl_delta?: Record<string, number>;
}
// One commit's own change (message + per-file table vs its git parent) — the
// commit-granular "progress" view in the Logs page.
export interface CommitChange {
  sha: string;
  short_sha: string;
  subject: string;
  created_at?: string;
  role?: string;
  kind: string; // 'agent' | 'integration' | …
  files: SessionChangeFile[];
  lean: ChangeRollup;
  blueprint: ChangeRollup;
  sorry_delta: number;
  other_count: number;
}
export interface SessionCommits {
  run: string;
  session: string;
  commits: CommitChange[];
  total?: number;
  offset?: number;
  next_offset?: number | null;
  has_more?: boolean;
}
export interface FileDiff { path: string; available: boolean; diff: string; truncated?: boolean }
// Raw interpolation (no encode): run ids/sessions/paths are bare and the path
// must match the Python endpoint registry exactly for static-mode hashing.
export const getSessionFileDiff = (runId: string, session: string, path: string, sha: string) =>
  getJson<FileDiff>(`/api/session/file-diff?run=${runId}&session=${session}&path=${path}&sha=${sha}`);
// Per-commit change view for one session (message + per-file table per commit).
export const getSessionCommits = (
  runId: string,
  session: string,
  offset?: number,
  limit?: number,
) => {
  let url = `/api/session/commits?run=${runId}&session=${session}`;
  if (offset !== undefined) url += `&offset=${offset}`;
  if (limit !== undefined) url += `&limit=${limit}`;
  return getJson<SessionCommits>(url);
};

export interface SearchResult {
  name: string | null;
  kind: string;
  signature: string;
  doc: string;
  library: string;
  file: string;
  line: number;
  score: number;
  blueprint: { project: string; id: string; title: string | null; statement: string; leanok: boolean } | null;
}
export const searchDeclarations = (
  q: string,
  mode: string,
  libraries?: string[],
  kinds?: string[],
  limit = 30,
) =>
  getJson<{ query: string; mode: string; indexed: number; count: number; results: SearchResult[] }>(
    `/api/search?q=${encodeURIComponent(q)}&mode=${encodeURIComponent(mode)}` +
      (libraries && libraries.length ? `&libs=${encodeURIComponent(libraries.join(','))}` : '') +
      (kinds && kinds.length ? `&kinds=${encodeURIComponent(kinds.join(','))}` : '') +
      `&limit=${limit}`,
  );

export async function editInbox(payload: Record<string, unknown>): Promise<any> {
  const res = await fetch('/api/inbox', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return parseJsonResponse<any>(res, 'inbox edit');
}

export async function editRoadmap(payload: Record<string, unknown>): Promise<any> {
  const res = await fetch('/api/roadmap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return parseJsonResponse<any>(res, 'roadmap edit');
}

export async function editTask(payload: Record<string, unknown>): Promise<any> {
  const res = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  return parseJsonResponse<any>(res, 'task edit');
}

export async function syncBlueprintDags(): Promise<any> {
  const res = await fetch('/api/blueprint/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
  return parseJsonResponse<any>(res, 'blueprint sync');
}
