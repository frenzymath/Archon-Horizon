import type { GitCommit } from './hooks/useGitLog';

// The transcript path string must match the Python endpoint registry exactly
// (it is what gets sha256-hashed in static mode), so ref is interpolated raw.
const transcriptPath = (ref: string) => `/api/transcript?ref=${ref}`;
const reportPath = (ref: string) => `/api/report?ref=${ref}`;

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`fetch ${url} -> ${res.status}`);
  return res.json() as Promise<T>;
}

export const getState = () => getJson<any>('/api/state');
export const getTranscripts = () => getJson<any[]>('/api/transcripts');
export const getTranscript = (ref: string) => getJson<any[]>(transcriptPath(ref));
export const getReport = (ref: string) => getJson<{ markdown: string }>(reportPath(ref));

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
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data?.error || `inbox edit -> ${res.status}`);
  }
  return data;
}

export async function editRoadmap(payload: Record<string, unknown>): Promise<any> {
  const res = await fetch('/api/roadmap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data?.error || `roadmap edit -> ${res.status}`);
  }
  return data;
}

export async function editTask(payload: Record<string, unknown>): Promise<any> {
  const res = await fetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data?.error || `task edit -> ${res.status}`);
  }
  return data;
}
