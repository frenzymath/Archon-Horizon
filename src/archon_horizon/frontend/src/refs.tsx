// Local-reference resolution: turn tokens that appear in rendered text (inbox
// bodies, commit messages, reports, roadmap summaries, comments) into clickable
// chips that navigate to the matching view. We only ever linkify a token that
// resolves against the KNOWN SET already in app state — never a blind guess — so
// prose can't sprout false chips.
import React, { createContext, useContext, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { getCommit } from './api';

export type RefKind = 'roadmap' | 'task' | 'inbox' | 'node' | 'commit';
export interface ResolvedRef {
  kind: RefKind;
  href: string;
  title: string;
}
export type RefResolver = (token: string) => ResolvedRef | null;

// ── roadmap / inbox metadata helpers ────────────────────────────────────────
// The board fields ride in item.metadata (owner/milestone/pinned_commits for
// roadmap items, owner_task/read_by for inbox items), mirroring core/roadmap.py
// and core/inbox.py — so the dashboard reads them the same way the CLI does.
export function itemOwner(item: any): string {
  const raw = item?.metadata?.owner;
  return typeof raw === 'string' ? raw.trim() : '';
}
export function itemMilestone(item: any): string {
  const raw = item?.metadata?.milestone;
  return typeof raw === 'string' ? raw.trim() : '';
}
export function itemPinnedCommits(item: any): string[] {
  const raw = item?.metadata?.pinned_commits;
  if (!Array.isArray(raw)) return [];
  return raw.map((s) => String(s).trim()).filter(Boolean);
}
export function inboxOwnerTask(item: any): string {
  const raw = item?.metadata?.owner_task;
  return typeof raw === 'string' ? raw.trim() : '';
}
export function inboxReadBy(item: any): string[] {
  const raw = item?.metadata?.read_by;
  if (!Array.isArray(raw)) return [];
  return raw.map((s) => String(s).trim()).filter(Boolean);
}

// A short, stable signature of every id/sha the resolver keys on, so the resolver
// identity only changes when the known set changes — not on every 5s poll. This
// keeps the memoized markdown render from re-running while ids are unchanged.
export function refSignature(state: any): string {
  const roadmap = (state?.roadmap?.items ?? []).map((i: any) => i.id);
  const tasks = (state?.tasks ?? []).map((t: any) => t.id);
  const inbox = [...(state?.local_inbox ?? []), ...(state?.github_inbox ?? [])].map((i: any) => i.id);
  const nodes: string[] = [];
  const pinned: string[] = [];
  for (const project of Object.keys(state?.blueprints ?? {})) {
    for (const n of state.blueprints[project]?.nodes ?? []) nodes.push(String(n.id));
  }
  for (const i of state?.roadmap?.items ?? []) pinned.push(...itemPinnedCommits(i));
  return [roadmap.length, tasks.length, inbox.length, nodes.length, pinned.length].join(':')
    + '|' + roadmap.join(',') + '|' + tasks.join(',') + '|' + inbox.join(',')
    + '|' + nodes.join(',') + '|' + pinned.join(',');
}

export function buildRefResolver(state: any): RefResolver {
  const roadmap = new Map<string, ResolvedRef>();
  for (const item of state?.roadmap?.items ?? []) {
    if (!item?.id) continue;
    roadmap.set(String(item.id), {
      kind: 'roadmap',
      href: `/roadmap?focus=${encodeURIComponent(item.id)}`,
      title: `${item.status ?? 'roadmap'} · ${item.title ?? item.id}`,
    });
  }
  const tasks = new Map<string, ResolvedRef>();
  for (const task of state?.tasks ?? []) {
    if (!task?.id) continue;
    tasks.set(String(task.id), {
      kind: 'task',
      href: `/tasks?task=${encodeURIComponent(task.id)}`,
      title: `${task.status ?? 'task'} · ${task.title || task.objective || task.id}`,
    });
  }
  const inbox = new Map<string, ResolvedRef>();
  for (const item of [...(state?.local_inbox ?? []), ...(state?.github_inbox ?? [])]) {
    if (!item?.id) continue;
    inbox.set(String(item.id), {
      kind: 'inbox',
      href: `/inbox?item=${encodeURIComponent(item.id)}`,
      title: `${item.title ?? item.id}`,
    });
  }
  // A global nodeId → project map so a bare blueprint node id links to its DAG.
  const nodes = new Map<string, ResolvedRef>();
  for (const project of Object.keys(state?.blueprints ?? {})) {
    for (const n of state.blueprints[project]?.nodes ?? []) {
      const id = String(n.id);
      if (!id || nodes.has(id)) continue;
      nodes.set(id, {
        kind: 'node',
        href: `/dag?project=${encodeURIComponent(project)}&focus=${encodeURIComponent(id)}`,
        title: `${n.title || id} · ${project}`,
      });
    }
  }
  // Commit SHAs are not globally indexed in state; we only linkify a hex token
  // that matches a known pinned commit (keyed by its first 7 chars, so both the
  // abbreviated and full form resolve). Rich subject/project come from the live
  // /api/commit resolver in <CommitChip>.
  const pinnedByPrefix = new Map<string, string>();
  for (const item of state?.roadmap?.items ?? []) {
    for (const sha of itemPinnedCommits(item)) {
      const s = sha.toLowerCase();
      if (s.length >= 7) pinnedByPrefix.set(s.slice(0, 7), sha);
    }
  }

  return (token: string): ResolvedRef | null => {
    if (!token) return null;
    if (roadmap.has(token)) return roadmap.get(token)!;
    if (tasks.has(token)) return tasks.get(token)!;
    if (inbox.has(token)) return inbox.get(token)!;
    if (nodes.has(token)) return nodes.get(token)!;
    if (/^[0-9a-fA-F]{7,40}$/.test(token)) {
      const full = pinnedByPrefix.get(token.toLowerCase().slice(0, 7));
      if (full) {
        return {
          kind: 'commit',
          href: `/logs?commit=${encodeURIComponent(full)}`,
          title: `commit ${full.slice(0, 12)}`,
        };
      }
    }
    return null;
  };
}

export function useRefResolver(state: any): RefResolver {
  const sig = refSignature(state);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  return useMemo(() => buildRefResolver(state), [sig]);
}

// ── context wiring ───────────────────────────────────────────────────────────
interface RefLinkValue { resolve: RefResolver; go: (href: string) => void }
const RefLinkContext = createContext<RefLinkValue | null>(null);

export function RefLinkProvider({ resolve, children }: { resolve: RefResolver; children: React.ReactNode }) {
  const navigate = useNavigate();
  const value = useMemo<RefLinkValue>(() => ({ resolve, go: (href) => navigate(href) }), [resolve, navigate]);
  return <RefLinkContext.Provider value={value}>{children}</RefLinkContext.Provider>;
}

export function useRefLinks(): RefLinkValue | null {
  return useContext(RefLinkContext);
}

// Delegated click handler for a rendered-HTML container: intercept clicks on the
// chip anchors we injected and route them through react-router instead of a full
// page load (which also keeps them working under HashRouter in static mode).
export function refChipClickHandler(go: (href: string) => void) {
  return (event: React.MouseEvent) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.button !== 0) return;
    const target = event.target as HTMLElement | null;
    const anchor = target?.closest?.('a[data-ref]') as HTMLAnchorElement | null;
    if (!anchor) return;
    const href = anchor.getAttribute('href');
    if (!href) return;
    event.preventDefault();
    go(href);
  };
}

// A single explicit reference chip (used where we render an id directly rather
// than inside markdown, e.g. the board's linked roadmap id).
export function RefChip({ token, label, className }: { token: string; label?: string; className?: string }) {
  const links = useRefLinks();
  const ref = links?.resolve(token) ?? null;
  if (!ref || !links) return <span className={className}>{label ?? token}</span>;
  return (
    <a
      className={`tag-chip tag-ref-${ref.kind} ref-chip ${className ?? ''}`}
      href={ref.href}
      title={ref.title}
      onClick={(e) => { e.preventDefault(); links.go(ref.href); }}
    >
      {label ?? token}
    </a>
  );
}

// A commit chip that self-resolves its subject/project from the live resolver,
// so a pinned SHA reads as "abc1234 · fix the thing" with a click into the logs.
export function CommitChip({ sha, className }: { sha: string; className?: string }) {
  const links = useRefLinks();
  const [info, setInfo] = React.useState<{ short_sha: string; subject: string; project: string } | null>(null);
  React.useEffect(() => {
    let cancelled = false;
    getCommit(sha).then((data) => { if (!cancelled) setInfo(data); }).catch(() => {});
    return () => { cancelled = true; };
  }, [sha]);
  const short = info?.short_sha || sha.slice(0, 7);
  const title = info ? `${info.short_sha} · ${info.subject}${info.project ? ` (${info.project})` : ''}` : `commit ${sha}`;
  const href = `/logs?commit=${encodeURIComponent(sha)}`;
  return (
    <a
      className={`tag-chip tag-ref-commit commit-chip ${className ?? ''}`}
      href={href}
      title={title}
      onClick={(e) => { e.preventDefault(); links?.go(href); }}
    >
      <span className="commit-chip-sha">{short}</span>
      {info?.subject && <span className="commit-chip-subject">{info.subject}</span>}
    </a>
  );
}
