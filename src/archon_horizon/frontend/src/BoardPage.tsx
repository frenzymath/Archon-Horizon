import { useMemo, useState } from 'react';
import { Panel, Status, InlineMarkdown } from './App';
import { itemOwner, itemMilestone, itemPinnedCommits, CommitChip, RefChip } from './refs';

type PageProps = { state: any; reload: () => void };

// Kanban columns, in the order the roadmap status vocabulary reads as a pipeline.
const BOARD_STATUSES = ['active', 'pending', 'blocked', 'done', 'rejected'] as const;
const STATUS_LABEL: Record<string, string> = {
  active: 'Active', pending: 'Pending', blocked: 'Blocked', done: 'Done', rejected: 'Rejected',
};
const UNASSIGNED = 'Unassigned';

function itemStatus(item: any): string {
  const s = String(item?.status ?? 'active');
  return (BOARD_STATUSES as readonly string[]).includes(s) ? s : 'active';
}

export default function BoardPage({ state }: PageProps) {
  const items: any[] = state.roadmap?.items ?? [];
  const tasks: any[] = state.tasks ?? [];

  // "Who is running what" is derived, never stored: a roadmap item pulses when a
  // running task points at it via roadmap_refs.
  const runningIds = useMemo(() => {
    const ids = new Set<string>();
    for (const task of tasks) {
      if (task.status !== 'running') continue;
      for (const ref of task.roadmap_refs ?? []) ids.add(String(ref));
    }
    return ids;
  }, [tasks]);

  // Group by milestone label (free string), preserving first-seen order but
  // floating "Unassigned" to the end so named milestones lead.
  const milestones = useMemo(() => {
    const order: string[] = [];
    const byLabel = new Map<string, any[]>();
    for (const item of items) {
      const label = itemMilestone(item) || UNASSIGNED;
      if (!byLabel.has(label)) { byLabel.set(label, []); order.push(label); }
      byLabel.get(label)!.push(item);
    }
    order.sort((a, b) => (a === UNASSIGNED ? 1 : b === UNASSIGNED ? -1 : a.localeCompare(b)));
    return order.map((label) => ({ label, items: byLabel.get(label)! }));
  }, [items]);

  const [milestoneFilter, setMilestoneFilter] = useState<string>('all');
  const visibleMilestones = milestoneFilter === 'all'
    ? milestones
    : milestones.filter((m) => m.label === milestoneFilter);

  return (
    <div className="page board-page">
      <Panel title="Board" subtitle={`${items.length} roadmap item${items.length === 1 ? '' : 's'} across ${milestones.length} milestone${milestones.length === 1 ? '' : 's'}`}>
        {items.length === 0 ? (
          <p className="empty">No roadmap items to board yet.</p>
        ) : (
          <>
            <div className="board-filter" role="group" aria-label="Milestone filter">
              <button
                type="button"
                className={`search-facet-chip tag-chip ${milestoneFilter === 'all' ? 'on tag-project' : ''}`}
                onClick={() => setMilestoneFilter('all')}
              >
                All milestones
              </button>
              {milestones.map((m) => (
                <button
                  key={m.label}
                  type="button"
                  className={`search-facet-chip tag-chip ${milestoneFilter === m.label ? 'on tag-project' : ''}`}
                  onClick={() => setMilestoneFilter(m.label)}
                >
                  {m.label === UNASSIGNED ? '◇ Unassigned' : `◇ ${m.label}`} <span className="board-count">{m.items.length}</span>
                </button>
              ))}
            </div>
            {visibleMilestones.map((m) => (
              <MilestoneBoard key={m.label} label={m.label} items={m.items} runningIds={runningIds} />
            ))}
          </>
        )}
      </Panel>
    </div>
  );
}

function MilestoneBoard({ label, items, runningIds }: { label: string; items: any[]; runningIds: Set<string> }) {
  const columns = useMemo(() => {
    const byStatus = new Map<string, any[]>();
    for (const s of BOARD_STATUSES) byStatus.set(s, []);
    for (const item of items) byStatus.get(itemStatus(item))!.push(item);
    for (const list of byStatus.values()) {
      list.sort((a, b) => String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
    }
    return byStatus;
  }, [items]);

  return (
    <section className="board-milestone">
      <div className="board-milestone-head">
        <h3>{label === UNASSIGNED ? 'Unassigned' : `◇ ${label}`}</h3>
        <span className="board-count">{items.length}</span>
      </div>
      <div className="board-columns">
        {BOARD_STATUSES.map((status) => {
          const cards = columns.get(status)!;
          return (
            <div key={status} className={`board-column board-column-${status}`}>
              <div className="board-column-head">
                <Status value={status} label={STATUS_LABEL[status]} />
                <span className="board-count">{cards.length}</span>
              </div>
              <div className="board-column-cards">
                {cards.map((item) => <BoardCard key={item.id} item={item} running={runningIds.has(String(item.id))} />)}
                {cards.length === 0 && <div className="board-column-empty">—</div>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function BoardCard({ item, running }: { item: any; running: boolean }) {
  const owner = itemOwner(item);
  const priority = item.priority || 'normal';
  const projects: string[] = item.projects ?? [];
  const dependsOn: string[] = item.depends_on ?? [];
  const pinned = itemPinnedCommits(item);
  const milestone = itemMilestone(item);
  const hasDetails = projects.length > 0 || dependsOn.length > 0 || pinned.length > 0 || !!milestone;

  return (
    <article className={`board-card${running ? ' board-card-running' : ''}`}>
      {/* Fixed, always-scannable header line: title · status · owner · priority. */}
      <div className="board-card-head">
        <span className="board-card-id">{item.id}</span>
        {running && <span className="board-running-badge" title="A running task is working this item">● live</span>}
        <span className={`priority-chip priority-${priority}`}>{priority}</span>
      </div>
      <div className="board-card-title"><InlineMarkdown content={item.title || item.id} /></div>
      <div className="board-card-meta">
        <Status value={itemStatus(item)} />
        {owner
          ? <span className="tag-chip tag-ref-owner board-owner">@{owner}</span>
          : <span className="board-owner board-owner-empty">unowned</span>}
      </div>
      {hasDetails && (
        <details className="board-card-details">
          <summary>details</summary>
          <div className="board-card-detail-body">
            {projects.length > 0 && (
              <div className="board-detail-row"><span>Projects</span><div>{projects.map((p) => <span key={p} className="tag-chip tag-project">{p}</span>)}</div></div>
            )}
            {dependsOn.length > 0 && (
              <div className="board-detail-row"><span>Depends on</span><div>{dependsOn.map((d) => <RefChip key={d} token={d} />)}</div></div>
            )}
            {pinned.length > 0 && (
              <div className="board-detail-row"><span>Commits</span><div className="board-commits">{pinned.map((sha) => <CommitChip key={sha} sha={sha} />)}</div></div>
            )}
            {milestone && (
              <div className="board-detail-row"><span>Milestone</span><div><span className="tag-chip tag-ref-milestone">◇ {milestone}</span></div></div>
            )}
          </div>
        </details>
      )}
    </article>
  );
}
