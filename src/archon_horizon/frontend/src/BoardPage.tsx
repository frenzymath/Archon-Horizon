import { useMemo, useState } from 'react';
import { Panel, Status, InlineMarkdown, RoadmapEditor } from './App';
import { editRoadmap } from './api';
import { itemOwner, itemMilestone, itemPinnedCommits, CommitChip, RefChip } from './refs';
import { isStaticDashboard } from './staticMode';
import { formatChipDateTime } from './utils/datetime';

type PageProps = { state: any; reload: () => void };

// The board is an operational projection, not a second copy of the roadmap.
// Blocked work waits in Pending; rejected roadmap entries are omitted entirely.
const BOARD_STATUSES = ['pending', 'active', 'done'] as const;
const STATIC = isStaticDashboard();
const STATUS_LABEL: Record<string, string> = {
  pending: 'Pending', active: 'Active', done: 'Done',
};

function itemStatus(item: any): string {
  const status = String(item?.status ?? 'pending');
  if (status === 'active') return 'active';
  if (status === 'done') return 'done';
  return 'pending';
}

function itemProjects(item: any): string[] {
  return Array.isArray(item?.projects) ? item.projects.map(String).filter(Boolean) : [];
}

function itemCreatedAt(item: any): string {
  return String(item?.activity?.created_at || item?.metadata?.created_at || '');
}

function itemActivityAt(item: any): string {
  return String(item?.activity?.updated_at || item?.metadata?.updated_at || itemCreatedAt(item));
}

function compactTime(value: string): string {
  return formatChipDateTime(value);
}

export default function BoardPage({ state, reload }: PageProps) {
  const allItems: any[] = state.roadmap?.items ?? [];
  const tasks: any[] = state.tasks ?? [];
  const [creating, setCreating] = useState(false);
  const [message, setMessage] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);
  const boardItems = useMemo(
    () => allItems.filter((item) => itemMilestone(item) && item.status !== 'rejected'),
    [allItems],
  );

  const tasksByRoadmap = useMemo(() => {
    const byId = new Map<string, any[]>();
    for (const task of tasks) {
      for (const rawRef of task.roadmap_refs ?? []) {
        const ref = String(rawRef);
        if (!byId.has(ref)) byId.set(ref, []);
        byId.get(ref)!.push(task);
      }
    }
    return byId;
  }, [tasks]);

  const milestones = useMemo(() => {
    const byLabel = new Map<string, any[]>();
    for (const item of boardItems) {
      const label = itemMilestone(item);
      if (!byLabel.has(label)) byLabel.set(label, []);
      byLabel.get(label)!.push(item);
    }
    return [...byLabel.keys()].sort().map((label) => ({ label, items: byLabel.get(label)! }));
  }, [boardItems]);

  const owners = useMemo(
    () => [...new Set(boardItems.map((item) => itemOwner(item) || 'unowned'))].sort(),
    [boardItems],
  );
  const projects = useMemo(
    () => [...new Set(boardItems.flatMap(itemProjects))].sort(),
    [boardItems],
  );

  const [milestoneFilter, setMilestoneFilter] = useState<string>('all');
  const [ownerFilter, setOwnerFilter] = useState<string>('all');
  const [projectFilter, setProjectFilter] = useState<string>('all');
  const visibleMilestones = milestones
    .filter((m) => milestoneFilter === 'all' || m.label === milestoneFilter)
    .map((m) => ({
      ...m,
      items: m.items.filter((item) => (
        (ownerFilter === 'all' || (itemOwner(item) || 'unowned') === ownerFilter)
        && (projectFilter === 'all' || itemProjects(item).includes(projectFilter))
      )),
    }))
    .filter((m) => m.items.length > 0);
  const visibleCount = visibleMilestones.reduce((sum, milestone) => sum + milestone.items.length, 0);
  const initialMilestone = milestoneFilter !== 'all'
    ? milestoneFilter
    : milestones.length === 1 ? milestones[0].label : '';
  const runAction = (payload: Record<string, unknown>, success?: string) => {
    setMessage(null);
    return editRoadmap(payload)
      .then((result) => {
        if (result?.ok === false) throw new Error('operation failed');
        if (success) setMessage({ kind: 'info', text: success });
        reload();
        return true;
      })
      .catch((error) => {
        setMessage({ kind: 'error', text: error.message || String(error) });
        return false;
      });
  };

  return (
    <div className="page board-page">
      <Panel title="Board" subtitle={`${boardItems.length} milestone-linked roadmap item${boardItems.length === 1 ? '' : 's'} across ${milestones.length} milestone${milestones.length === 1 ? '' : 's'}`}>
        <div className="board-filter" role="group" aria-label="Board filters">
          {boardItems.length > 0 && <>
              <label>Milestone
                <select value={milestoneFilter} onChange={(event) => setMilestoneFilter(event.target.value)}>
                  <option value="all">All milestones</option>
                  {milestones.map((m) => <option key={m.label} value={m.label}>{m.label} ({m.items.length})</option>)}
                </select>
              </label>
              <label>Owner
                <select value={ownerFilter} onChange={(event) => setOwnerFilter(event.target.value)}>
                  <option value="all">All owners</option>
                  {owners.map((owner) => <option key={owner} value={owner}>{owner}</option>)}
                </select>
              </label>
              <label>Project
                <select value={projectFilter} onChange={(event) => setProjectFilter(event.target.value)}>
                  <option value="all">All projects</option>
                  {projects.map((project) => <option key={project} value={project}>{project}</option>)}
                </select>
              </label>
              <span className="board-filter-result">{visibleCount} shown</span>
          </>}
          {!STATIC && <button className="primary board-add-inline" type="button" onClick={() => setCreating((open) => !open)}>
            {creating ? 'Close editor' : '+ Add item'}
          </button>}
        </div>
        {!STATIC && creating && (
          <div className="roadmap-editor-shell board-editor-shell">
            <RoadmapEditor
              runAction={runAction}
              projects={state.projects ?? []}
              parentOptions={allItems.map((item) => String(item.id))}
              initialMilestone={initialMilestone}
              requireMilestone
              onCancel={() => setCreating(false)}
              onSaved={() => setCreating(false)}
            />
          </div>
        )}
        {message && <div className={`notice ${message.kind}`}>{message.text}</div>}
        {boardItems.length === 0 ? (
          <p className="empty">No non-rejected roadmap items are linked to a milestone.</p>
        ) : (
          <>
            {visibleMilestones.map((m) => (
              <MilestoneBoard key={m.label} label={m.label} items={m.items} tasksByRoadmap={tasksByRoadmap} />
            ))}
            {visibleCount === 0 && <p className="empty">No board items match these filters.</p>}
          </>
        )}
      </Panel>
    </div>
  );
}

function MilestoneBoard({ label, items, tasksByRoadmap }: { label: string; items: any[]; tasksByRoadmap: Map<string, any[]> }) {
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
        <h3>◇ {label}</h3>
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
                {cards.map((item) => {
                  const linkedTasks = tasksByRoadmap.get(String(item.id)) ?? [];
                  return <BoardCard key={item.id} item={item} linkedTasks={linkedTasks} running={linkedTasks.some((task) => task.status === 'running')} />;
                })}
                {cards.length === 0 && <div className="board-column-empty">—</div>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function BoardCard({ item, linkedTasks, running }: { item: any; linkedTasks: any[]; running: boolean }) {
  const owner = itemOwner(item);
  const priority = item.priority || 'normal';
  const projects = itemProjects(item);
  const dependsOn: string[] = item.depends_on ?? [];
  const pinned = itemPinnedCommits(item);
  const createdAt = itemCreatedAt(item);
  const activityAt = itemActivityAt(item);
  const rolledUpTasks = Number(item.activity?.task_count || linkedTasks.length);
  const hasDetails = projects.length > 0 || dependsOn.length > 0 || pinned.length > 0 || linkedTasks.length > 0;

  return (
    <article className={`board-card${running ? ' board-card-running' : ''}`}>
      {/* Fixed, always-scannable header line: title · status · owner · priority. */}
      <div className="board-card-head">
        <RefChip token={String(item.id)} className="board-card-id" />
        {running && <span className="board-running-badge" title="A running task is working this item">● live</span>}
        <span className={`priority-chip priority-${priority}`}>{priority}</span>
      </div>
      <div className="board-card-title"><InlineMarkdown content={item.title || item.id} /></div>
      <div className="board-card-meta">
        {owner
          ? <span className="tag-chip tag-ref-owner board-owner">@{owner}</span>
          : <span className="board-owner board-owner-empty">unowned</span>}
        {rolledUpTasks > 0 && <span className="board-task-count">{rolledUpTasks} task{rolledUpTasks === 1 ? '' : 's'}</span>}
        {item.status === 'blocked' && <span className="board-source-status" title="Roadmap status: blocked">blocked</span>}
        {activityAt && (
          <time className="board-card-time" title="Latest roadmap, child, or linked-task activity">
            activity {compactTime(activityAt)}
          </time>
        )}
      </div>
      {hasDetails && (
        <details className="board-card-details">
          <summary>details</summary>
          <div className="board-card-detail-body">
            {createdAt && (
              <div className="board-detail-row"><span>Created</span><div>{compactTime(createdAt)}{item.activity?.created_at_inferred ? ' (inferred)' : ''}</div></div>
            )}
            {projects.length > 0 && (
              <div className="board-detail-row"><span>Projects</span><div>{projects.map((p) => <span key={p} className="tag-chip tag-project">{p}</span>)}</div></div>
            )}
            {dependsOn.length > 0 && (
              <div className="board-detail-row"><span>Depends on</span><div>{dependsOn.map((d) => <RefChip key={d} token={d} />)}</div></div>
            )}
            {pinned.length > 0 && (
              <div className="board-detail-row"><span>Commits</span><div className="board-commits">{pinned.map((sha) => <CommitChip key={sha} sha={sha} />)}</div></div>
            )}
            {linkedTasks.length > 0 && (
              <div className="board-detail-row"><span>Tasks</span><div>{linkedTasks.map((task) => <RefChip key={task.id} token={String(task.id)} />)}</div></div>
            )}
          </div>
        </details>
      )}
    </article>
  );
}
