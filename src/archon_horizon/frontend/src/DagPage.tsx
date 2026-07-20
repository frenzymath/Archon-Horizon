import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import DagNetwork from './components/DagNetwork';
import { buildBlueprintModel, TexFragment } from './components/BlueprintDoc';
import ProjectPicker from './components/ProjectPicker';
import { syncBlueprintDags, getBlueprintChapters, getBlueprintDag, type BlueprintChaptersResponse, type BlueprintDagResponse } from './api';
import { isStaticDashboard } from './staticMode';

type Query = 'all' | 'frontier' | 'unproved' | 'sorry' | 'gaps' | 'leanok' | 'mathlib' | 'roots' | 'leaves' | 'isolated';

const QUERY_LABEL: Record<Query, string> = {
  all: 'all nodes', frontier: 'frontier (ready to prove)', unproved: 'unproved', sorry: 'has sorry',
  gaps: 'missing Lean link', leanok: 'lean ok', mathlib: 'in mathlib',
  roots: 'roots (no deps)', leaves: 'leaves (unused)', isolated: 'isolated',
};

const proved = (n: any) => n.proved ?? n.leanok ?? false;
const mathlib = (n: any) => n.mathlib_ok ?? n.mathlibok ?? false;
const isDone = (n: any) => proved(n) || mathlib(n);
const leanName = (n: any) => n.lean_name ?? n.lean ?? null;
const nodeType = (n: any) => n.type ?? n.kind ?? 'node';
const fileOf = (n: any) => n.lean_file ?? n.tex_file ?? '';
const STATIC = isStaticDashboard();

const DEP_PREVIEW = 6;
function DepList({ items, empty, onGoTo }: { items: string[]; empty: string; onGoTo: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  useEffect(() => setExpanded(false), [items.join('|')]);
  if (!items.length) return <div className="deps-list"><span className="no-deps">{empty}</span></div>;
  const shown = expanded ? items : items.slice(0, DEP_PREVIEW);
  return (
    <div className="deps-list">
      {shown.map((u) => <span key={u} className="dep-chip" onClick={() => onGoTo(u)}>{u}</span>)}
      {items.length > DEP_PREVIEW && (
        <button className="dep-more" onClick={() => setExpanded((v) => !v)}>{expanded ? '− less' : `+${items.length - shown.length} more`}</button>
      )}
    </div>
  );
}

function DualRange({ label, max, value, onChange, infiniteTop }: {
  label: string; max: number; value: [number, number]; onChange: (v: [number, number]) => void;
}) {
  const [lo, hi] = value;
  const hiVal = hi === Infinity ? max : hi;
  const active = lo > 0 || (hi !== Infinity && hi < max);
  if (max <= 0) return null;
  return (
    <div className={`dv-range ${active ? 'dv-range-on' : ''}`}>
      <span className="dv-range-lbl">{label}</span>
      <input className="dv-slider" type="range" min={0} max={max} value={lo} onChange={(e) => onChange([Math.min(Number(e.target.value), hiVal), hi])} />
      <input className="dv-slider" type="range" min={0} max={max} value={hiVal} onChange={(e) => { const v = Number(e.target.value); onChange([Math.min(lo, v), v >= max ? Infinity : v]); }} />
      <span className="dv-range-val">{lo}–{hi === Infinity ? max : hi}</span>
    </div>
  );
}

export default function DagPage({ state, reload }: { state: any; reload?: () => void }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedProject = searchParams.get('project') ?? '';
  const focusNode = searchParams.get('focus') ?? '';
  const projects: string[] = Object.keys(state.blueprints ?? {});
  const [project, setProject] = useState(projects[0] ?? '');
  // Fetch the project's blueprint chapters so node statements/proofs render
  // through the same parser as the Blueprint page: custom macros, refs, and
  // text-mode environments such as itemize/enumerate.
  const [blueprintData, setBlueprintData] = useState<BlueprintChaptersResponse | null>(null);
  useEffect(() => {
    if (!project) { setBlueprintData(null); return; }
    let cancelled = false;
    getBlueprintChapters(project)
      .then((data) => { if (!cancelled) setBlueprintData(data); })
      .catch(() => { if (!cancelled) setBlueprintData(null); });
    return () => { cancelled = true; };
  }, [project]);
  const macros = blueprintData?.macros ?? {};
  const labelMap = useMemo(() => buildBlueprintModel(blueprintData?.chapters ?? [], true).labels, [blueprintData]);
  // The full DAG (with node statements / proofs / Lean source) is fetched on
  // demand rather than ridden along in the 5s /api/state poll — that heavy text
  // is only needed here and on the Blueprint page.
  const [fullDag, setFullDag] = useState<BlueprintDagResponse | null>(null);
  useEffect(() => {
    if (!project) { setFullDag(null); return; }
    let cancelled = false;
    getBlueprintDag(project)
      .then((data) => { if (!cancelled) setFullDag(data); })
      .catch(() => { if (!cancelled) setFullDag(null); });
    return () => { cancelled = true; };
  }, [project]);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState<Query>('all');
  const [search, setSearch] = useState('');
  const [typeSel, setTypeSel] = useState('');
  const [chapterSel, setChapterSel] = useState('');
  const [fileSel, setFileSel] = useState('');
  const [depRange, setDepRange] = useState<[number, number]>([0, Infinity]);
  const [statsOpen, setStatsOpen] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [syncMessage, setSyncMessage] = useState('');

  useEffect(() => {
    if (requestedProject && projects.includes(requestedProject) && project !== requestedProject) {
      setProject(requestedProject);
    } else if (project && !projects.includes(project)) setProject(projects[0] ?? '');
    else if (!project && projects[0]) setProject(projects[0]);
  }, [projects.join('|'), requestedProject, project]);
  useEffect(() => {
    setSelected(focusNode || null); setQuery('all'); setTypeSel(''); setChapterSel(''); setFileSel('');
    setDepRange([0, Infinity]);
  }, [project, focusNode]);

  // Prefer the freshly-fetched full DAG; fall back to the light DAG from the
  // poll (nodes/edges without the heavy text) so the graph still draws instantly
  // while the full payload is loading.
  const dag: any = (project ? (fullDag ?? state.blueprints?.[project]) : null) ?? {};
  const dagSig = useMemo(() => JSON.stringify(dag?.nodes ?? []) + '|' + JSON.stringify(dag?.edges ?? []), [dag]);
  const nodes: any[] = useMemo(() => dag.nodes ?? [], [dagSig]); // eslint-disable-line react-hooks/exhaustive-deps
  const edges: any[] = useMemo(() => dag.edges ?? [], [dagSig]); // eslint-disable-line react-hooks/exhaustive-deps
  const meta = dag.meta ?? {};

  const depsMap = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const e of edges) (m.get(e.target) ?? (m.set(e.target, []), m.get(e.target)!)).push(e.source);
    return m;
  }, [edges]);
  const usedByMap = useMemo(() => {
    const m = new Map<string, string[]>();
    for (const e of edges) (m.get(e.source) ?? (m.set(e.source, []), m.get(e.source)!)).push(e.target);
    return m;
  }, [edges]);
  const byId = useMemo(() => new Map(nodes.map((n) => [n.id, n])), [nodes]);
  const graphNodes = nodes;
  const depCount = (n: any) => n.dep_count ?? (n.uses ?? depsMap.get(n.id) ?? []).length;
  const rdepCount = (n: any) => n.rdep_count ?? (usedByMap.get(n.id) ?? []).length;
  const doneIds = useMemo(() => new Set(nodes.filter(isDone).map((n) => n.id)), [nodes]);

  const types = useMemo(() => [...new Set(nodes.map(nodeType))].filter(Boolean).sort(), [nodes]);
  const chapters = useMemo(() => [...new Set(nodes.map((n) => n.chapter).filter(Boolean))].sort(), [nodes]);
  const files = useMemo(() => [...new Set(nodes.map(fileOf).filter(Boolean))].sort(), [nodes]);
  const maxDep = useMemo(() => nodes.reduce((m, n) => Math.max(m, depCount(n)), 0), [nodes]); // eslint-disable-line react-hooks/exhaustive-deps

  const node = useMemo(() => byId.get(selected ?? '') ?? null, [byId, selected]);
  useEffect(() => {
    if (focusNode && byId.has(focusNode)) setSelected(focusNode);
  }, [focusNode, byId]);
  const directDeps = useMemo(() => (selected ? depsMap.get(selected) ?? [] : []), [depsMap, selected]);
  const usedBy = useMemo(() => (selected ? usedByMap.get(selected) ?? [] : []), [usedByMap, selected]);
  const ancestors = useMemo(() => {
    if (!selected) return new Set<string>();
    const seen = new Set<string>();
    const stack = [...(depsMap.get(selected) ?? [])];
    while (stack.length) {
      const id = stack.pop()!;
      if (seen.has(id)) continue;
      seen.add(id);
      stack.push(...(depsMap.get(id) ?? []));
    }
    return seen;
  }, [byId, depsMap, selected]);
  const indirect = useMemo(() => [...ancestors].filter((x) => !directDeps.includes(x)).sort(), [ancestors, directDeps]);

  // Project metadata — ported from Archon's DagView `stats`.
  const stats = useMemo(() => {
    const bp = nodes;
    const ready = bp.filter((n) => !isDone(n) && (depsMap.get(n.id) ?? []).every((d: string) => !byId.has(d) || doneIds.has(d))).length;
    return {
      bpN: bp.length,
      completeN: bp.filter(isDone).length,
      provedN: bp.filter(proved).length,
      mathlib: bp.filter(mathlib).length,
      sorry: nodes.filter((n) => n.has_sorry).length,
      ready,
      gaps: bp.filter((n) => !leanName(n) && !mathlib(n)).length,
      leaves: nodes.filter((n) => rdepCount(n) === 0).length,
      roots: nodes.filter((n) => depCount(n) === 0).length,
      isolated: nodes.filter((n) => depCount(n) === 0 && rdepCount(n) === 0).length,
      pct: bp.length ? Math.round((100 * bp.filter(isDone).length) / bp.length) : 0,
    };
  }, [nodes, byId, depsMap, doneIds]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleQuery = (q: Query) => setQuery((cur) => (cur === q ? 'all' : q));

  const highlight = useMemo<Set<string> | null>(() => {
    const q = search.trim().toLowerCase();
    const rangesOn = depRange[0] > 0 || depRange[1] !== Infinity;
    if (query === 'all' && !q && !typeSel && !chapterSel && !fileSel && !rangesOn) return null;
    const matchText = (n: any) => !q || `${n.id} ${n.title ?? ''} ${leanName(n) ?? ''}`.toLowerCase().includes(q);
    const matchQuery = (n: any) => {
      switch (query) {
        case 'leanok': return proved(n);
        case 'mathlib': return mathlib(n);
        case 'unproved': return !isDone(n);
        case 'sorry': return !!n.has_sorry;
        case 'gaps': return !leanName(n) && !mathlib(n);
        case 'frontier': return !isDone(n) && (depsMap.get(n.id) ?? []).every((d: string) => !byId.has(d) || doneIds.has(d));
        case 'roots': return depCount(n) === 0;
        case 'leaves': return rdepCount(n) === 0;
        case 'isolated': return depCount(n) === 0 && rdepCount(n) === 0;
        default: return true;
      }
    };
    const matchRanges = (n: any) => {
      const dc = depCount(n);
      if (dc < depRange[0] || dc > depRange[1]) return false;
      return true;
    };
    return new Set(nodes.filter((n) =>
      matchText(n) && matchQuery(n)
      && (!typeSel || nodeType(n) === typeSel)
      && (!chapterSel || n.chapter === chapterSel)
      && (!fileSel || fileOf(n) === fileSel)
      && matchRanges(n)).map((n) => n.id));
  }, [nodes, byId, depsMap, usedByMap, doneIds, query, search, typeSel, chapterSel, fileSel, depRange]); // eslint-disable-line react-hooks/exhaustive-deps

  const visible = highlight ? graphNodes.filter((n) => highlight.has(n.id)).length : graphNodes.length;
  const dupCount = (meta.duplicate_ids ?? []).length;

  const qRow = (label: string, value: number, q: Query, vClass = '') => (
    <div className={`row dv-qrow ${query === q ? 'dv-qon' : ''}`} onClick={() => toggleQuery(q)} title={`Filter: ${QUERY_LABEL[q]}`}>
      <span>{label}</span><span className={`v ${vClass}`}>{value}</span>
    </div>
  );

  const statusBadge = node && (
    proved(node) ? <span className="badge badge-proved">✓ leanok</span>
      : mathlib(node) ? <span className="badge badge-mathlib">ⓜ mathlib</span>
      : node.has_sorry ? <span className="badge badge-sorry">sorry</span>
      : <span className="badge badge-unproved">unproved</span>
  );

  const openBlueprint = (n: any) => {
    navigate(`/blueprint?project=${encodeURIComponent(project)}&focus=${encodeURIComponent(n.id)}`);
  };
  const openLean = (n: any) => {
    const file = n.lean_file;
    if (!file) return;
    const qs = new URLSearchParams({ project, file: String(file) });
    const lean = leanName(n);
    if (lean) qs.set('decl', String(lean));
    navigate(`/lean?${qs.toString()}`);
  };
  const openBlueprintRef = (slug: string, anchor: string) => {
    const qs = new URLSearchParams({ project });
    if (slug) qs.set('slug', slug);
    if (anchor) qs.set('anchor', anchor);
    navigate(`/blueprint?${qs.toString()}`);
  };
  const syncBlueprint = () => {
    if (isStaticDashboard()) {
      setSyncMessage('Blueprint sync needs the live dashboard.');
      return;
    }
    setSyncMessage('');
    setSyncing(true);
    syncBlueprintDags()
      .then((result) => {
        const count = result?.projects?.length ?? 0;
        setSyncMessage(count ? `Synced ${count} project${count === 1 ? '' : 's'}` : 'No parseable blueprints found');
        reload?.();
      })
      .catch((error) => setSyncMessage(error?.message || String(error)))
      .finally(() => setSyncing(false));
  };

  return (
    <div className="page full-page" style={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      {projects.length === 0 ? (
        <p className="empty">No parseable blueprints. Configure project blueprint paths to populate this page.</p>
      ) : (
        <div className="dv-root" style={{ display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
          <div className="dv-toolbar">
            <span className="dv-brand">Dependency Graph</span>
            <span className="dv-stat">{visible}/{graphNodes.length} nodes · {edges.length} edges{highlight ? ` · ${highlight.size} highlighted` : ''}{meta.entry ? ` · ${String(meta.entry).split('/').pop()}` : ''}{dupCount ? ` · ⚠ ${dupCount} dup` : ''}</span>
            <span className="dv-legend">
              <span className="leg-box" style={{ background: '#66bb6a', borderColor: '#2e7d32' }} /><span className="leg-txt">closed</span>
              <span className="leg-box" style={{ background: '#bbdefb', borderColor: '#1565c0' }} /><span className="leg-txt">ready</span>
              <span className="leg-box" style={{ background: '#ffcc80', borderColor: '#2e7d32' }} /><span className="leg-txt">sorry</span>
              <span className="leg-box" style={{ background: '#ede9fe', borderColor: '#7c3aed' }} /><span className="leg-txt">chapter</span>
            </span>
            <button className="dv-sync" type="button" onClick={syncBlueprint} disabled={STATIC || syncing} title={STATIC ? 'Blueprint sync needs the live dashboard.' : 'Refresh the published rich blueprint DAG cache.'}>
              {syncing ? 'Syncing...' : 'Sync blueprint'}
            </button>
            {syncMessage && <span className={`dv-sync-msg ${syncMessage.startsWith('fetch') || syncMessage.includes('->') ? 'error' : ''}`}>{syncMessage}</span>}
            <span style={{ marginLeft: 'auto' }}><ProjectPicker projects={projects} value={project} onChange={setProject} /></span>
          </div>
          <div className="dv-controls">
            <input className="dv-input" type="search" placeholder="search id / title / lean…" value={search} onChange={(e) => setSearch(e.target.value)} />
            <select className={`dv-select ${query !== 'all' ? 'dv-select-on' : ''}`} value={query} onChange={(e) => setQuery(e.target.value as Query)}>
              {(Object.keys(QUERY_LABEL) as Query[]).map((q) => <option key={q} value={q}>{QUERY_LABEL[q]}</option>)}
            </select>
            {types.length > 1 && (
              <select className={`dv-select ${typeSel ? 'dv-select-on' : ''}`} value={typeSel} onChange={(e) => setTypeSel(e.target.value)}>
                <option value="">any type</option>{types.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
            )}
            {chapters.length > 0 && (
              <select className={`dv-select ${chapterSel ? 'dv-select-on' : ''}`} value={chapterSel} onChange={(e) => setChapterSel(e.target.value)}>
                <option value="">all chapters</option>{chapters.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            )}
            {files.length > 1 && (
              <select className={`dv-select ${fileSel ? 'dv-select-on' : ''}`} value={fileSel} onChange={(e) => setFileSel(e.target.value)}>
                <option value="">any file</option>{files.map((f) => <option key={f} value={f}>{String(f).split('/').pop()}</option>)}
              </select>
            )}
            <DualRange label="deps" max={maxDep} value={depRange} onChange={setDepRange} />
          </div>

          <div className="dv-main" style={{ position: 'relative', display: 'flex', flex: 1, minHeight: 0, gap: 0 }}>
            <div style={{ flex: 1, minWidth: 0, position: 'relative' }}>
              <DagNetwork nodes={graphNodes} edges={edges} selected={selected} onSelectNode={setSelected} highlight={highlight} />
              {statsOpen ? (
                <div className="dv-stats">
                  <div className="dv-stats-head"><h4>Project</h4><button className="dv-stats-toggle" title="Minimize" onClick={() => setStatsOpen(false)}>–</button></div>
                  <div className="row"><span>Complete</span><span className="v done">{stats.completeN}/{stats.bpN} · {stats.pct}%</span></div>
                  <div className="bar"><span style={{ width: `${stats.pct}%` }} /></div>
                  <div className="row"><span>Lean-proved</span><span className="v done">{stats.provedN}</span></div>
                  <div className="row"><span>Mathlib-backed</span><span className="v mathlib">{stats.mathlib}</span></div>
                  {qRow('With sorry', stats.sorry, 'sorry', stats.sorry ? 'inf' : '')}
                  {qRow('Ready to formalize', stats.ready, 'frontier')}
                  <div className="row"><span>Needs \lean{'{}'}</span><span className="v">{stats.gaps}</span></div>
                  <div className="sep" />
                  <h4>Structure</h4>
                  {qRow('Sinks', stats.leaves, 'leaves')}
                  {qRow('Sources', stats.roots, 'roots')}
                  {qRow('Isolated', stats.isolated, 'isolated', stats.isolated ? 'inf' : '')}
                </div>
              ) : (
                <button className="dv-stats-show" onClick={() => setStatsOpen(true)}>▸ Stats · {stats.pct}% done</button>
              )}
            </div>

            <aside style={{ width: 360, flexShrink: 0, overflowY: 'auto', borderLeft: '1px solid var(--border)', background: 'var(--bg-secondary)' }}>
              {!node ? (
                <div className="dv-sidebar-content">
                  <div className="card dv-help-card">
                    <div className="card-title">Using the DAG</div>
                    <p>Select a chapter to expand it, then select a node to inspect its statement, Lean link, dependency cone, and downstream users.</p>
                    <p>Use the toolbar to search by label/title/Lean name, filter to ready or blocked work, and highlight effort or dependency ranges.</p>
                    <p>After selecting a node, the sidebar exposes jumps into the full Blueprint reader and the matching Lean source file when the graph has those links.</p>
                  </div>
                </div>
              ) : (
                <div className="dv-sidebar-content">
                  <div className="card">
                    <div className="node-badges"><span className="badge badge-type">{String(nodeType(node)).toUpperCase()}</span>{statusBadge}</div>
                    <div className="node-title">{node.title || node.id}</div>
                    <div className="node-id">{node.id}</div>
                    {node.chapter && <div className="node-chapter">§ {node.chapter}</div>}
                    {leanName(node) && <div className="lean-ref">Lean: <code>{leanName(node)}</code></div>}
                    <div className="node-actions">
                      <button className="btn-focus" onClick={() => openBlueprint(node)}>Open in blueprint</button>
                      {node.lean_file && <button className="btn-focus" onClick={() => openLean(node)}>Open in Lean</button>}
                    </div>
                  </div>
                  <div className="card">
                    <div className="card-title">Structure</div>
                    <div className="degrees">
                      <div className="degree"><span className="degree-val">{depCount(node)}</span><span className="degree-label">direct deps</span></div>
                      <div className="degree"><span className="degree-val">{ancestors.size}</span><span className="degree-label">total upstream</span></div>
                      <div className="degree"><span className="degree-val">{rdepCount(node)}</span><span className="degree-label">used by</span></div>
                    </div>
                    <div className="deps-sub">direct dependencies</div>
                    <DepList items={directDeps} empty="none — source" onGoTo={setSelected} />
                    <div className="deps-sub">indirect (transitive) dependencies</div>
                    <DepList items={indirect} empty="none beyond the direct ones" onGoTo={setSelected} />
                  </div>
                  {node.statement && (
                    <div className="card">
                      <div className="card-title">LaTeX statement</div>
                      <div className="latex-rendered">
                        <TexFragment tex={node.statement} macros={macros} labels={labelMap} onNavigate={openBlueprintRef} />
                      </div>
                    </div>
                  )}
                  {node.proof_tex && (
                    <div className="card">
                      <div className="card-title">LaTeX proof</div>
                      <div className="latex-rendered">
                        <TexFragment tex={String(node.proof_tex).trim()} macros={macros} labels={labelMap} onNavigate={openBlueprintRef} />
                      </div>
                    </div>
                  )}
                  <div className="card">
                    <div className="card-title">Lean code</div>
                    {node.lean_source
                      ? <pre className="code-block">{node.lean_source}</pre>
                      : <pre className="code-block" style={{ fontStyle: 'italic', opacity: 0.7 }}>{leanName(node) ? 'source not captured (run `horizon graph sync`)' : 'no Lean declaration linked'}</pre>}
                  </div>
                </div>
              )}
            </aside>
          </div>
        </div>
      )}
    </div>
  );
}
