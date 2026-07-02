import type { GitCommit } from './hooks/useGitLog';

// The transcript path string must match the Python endpoint registry exactly
// (it is what gets sha256-hashed in static mode), so ref is interpolated raw.
const transcriptPath = (ref: string) => `/api/transcript?ref=${ref}`;
const reportPath = (ref: string) => `/api/report?ref=${ref}`;

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  return parseJsonResponse<T>(res, url);
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
export const getProjects = () =>
  getJson<{ projects: ProjectStat[]; totals: Omit<ProjectStat, 'name'> }>('/api/projects');
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
export interface SessionChange {
  session: string;
  role?: string;
  projects?: string[];
  available: boolean;
  reason?: string;
  initial?: boolean;
  worktree?: boolean;
  base_source?: 'previous-session' | 'git-parent' | 'working-tree' | 'session-commits' | 'none';
  scope_files?: string[];
  commits?: { sha: string; subject: string }[];
  sha?: string | null;
  files: SessionChangeFile[];
  lean: ChangeRollup;
  blueprint: ChangeRollup;
  other_count: number;
  // Back-compat Lean roll-ups (used by the run trend).
  loc_add: number;
  loc_del: number;
  sorry_delta: number;
  lean_files_changed: number;
}
export interface RunChanges {
  run: string;
  sessions: SessionChange[];
  trend: {
    session: string;
    role?: string;
    sorry_delta: number;
    cumulative_sorry_delta: number;
    loc_code_delta: number;
  }[];
  file_trends: Record<string, { session: string; sorry_after: number; loc_code_after: number }[]>;
}
export interface FileDiff { path: string; available: boolean; diff: string; truncated?: boolean }
// Raw interpolation (no encode): run ids/sessions/paths are bare and the path
// must match the Python endpoint registry exactly for static-mode hashing.
export const getRunChanges = (runId: string) =>
  getJson<RunChanges>(`/api/run/changes?run=${runId}`);
// Live-only (no working tree in a static export): current uncommitted changes
// of a running session vs the run's last committed session.
export const getWorkingChanges = (runId: string) =>
  getJson<SessionChange>(`/api/run/working-changes?run=${runId}`);
export const getSessionFileDiff = (runId: string, session: string, path: string, worktree = false) =>
  getJson<FileDiff>(`/api/session/file-diff?run=${runId}&session=${session}&path=${path}${worktree ? '&worktree=1' : ''}`);

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
