import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getBlueprintChapters, getBlueprintDag, type BlueprintChaptersResponse, type BlueprintDagResponse } from './api';
import { BibliographyView, bibMapFrom, buildBlueprintModel, ChapterView, TitleInline } from './components/BlueprintDoc';
import ProjectPicker from './components/ProjectPicker';
import styles from './BlueprintPage.module.css';

type BlueprintDeclStatus = 'leanok' | 'mathlibok' | 'sorry' | 'none';
type BlueprintDeclIndexItem = {
  label: string;
  kind: string;
  envName: string;
  num: string;
  slug: string;
  anchor: string;
  title: string;
  leanNames: string[];
  status: BlueprintDeclStatus;
  graphId?: string;
};

// Documentation/proof environments are renderable, but are not formalisation
// obligations. They must not create a status square or a TODO row in the index.
const NO_STATUS_KINDS = new Set([
  'remark', 'notation', 'convention', 'example', 'conjecture', 'claim', 'fact',
  'exercise', 'note', 'proof', 'proposition_',
]);

const STATUS_LABEL: Record<BlueprintDeclStatus, string> = {
  leanok: '✓ leanok',
  mathlibok: 'ⓜ mathlib',
  sorry: '△ sorry',
  none: 'todo',
};

// One color per status, everywhere squares/segments appear (hgraph's palette).
const STATUS_COLOR: Record<BlueprintDeclStatus, string> = {
  mathlibok: '#0B5FD0',
  leanok: '#137333',
  sorry: '#C2410C',
  none: '#6B7280',
};
const STATUS_ORDER: BlueprintDeclStatus[] = ['mathlibok', 'leanok', 'sorry', 'none'];

function proved(n: any) { return n?.proved ?? n?.leanok ?? false; }
function mathlib(n: any) { return n?.mathlib_ok ?? n?.mathlibok ?? false; }
function sorried(n: any) { return Boolean(n?.has_sorry ?? (n?.lean_status === 'sorry')); }

function gatherDecls(
  blocks: any[],
  labels: Map<string, { kind: string; num: string; slug: string; anchor: string }>,
  dagById: Map<string, any>,
): BlueprintDeclIndexItem[] {
  const out: BlueprintDeclIndexItem[] = [];
  const visit = (block: any) => {
    if (!block) return;
    // A proof/prose block is document content, not a declaration container.
    // Do not walk into it: labels and metadata in proof-side equations (or in
    // an embedded example) must never become status/TODO rows for the parent
    // interface.
    if (block.t === 'env') {
      const envName = String(block.name ?? 'env').replace(/[^A-Za-z0-9_-]/g, '_');
      if (NO_STATUS_KINDS.has(envName)) return;
    }
    if (block.t === 'env' && block.meta?.label) {
      const label = String(block.meta.label);
      const envName = String(block.name ?? 'env').replace(/[^A-Za-z0-9_-]/g, '_');
      if (!NO_STATUS_KINDS.has(envName)) {
        const target = labels.get(label);
        const dag = dagById.get(label);
        const isMathlib = Boolean(block.meta.mathlibok) || mathlib(dag);
        const isLeanOk = Boolean(block.meta.leanok) || proved(dag);
        const isSorry = !isMathlib && !isLeanOk && sorried(dag);
        out.push({
          label,
          kind: target?.kind ?? block.name ?? 'Declaration',
          envName,
          num: target?.num ?? '',
          slug: target?.slug ?? '',
          anchor: target?.anchor ?? block.anchor ?? '',
          title: String(block.meta.human ?? ''),
          leanNames: (block.meta.lean ?? []).map(String),
          status: isMathlib ? 'mathlibok' : isLeanOk ? 'leanok' : isSorry ? 'sorry' : 'none',
          graphId: dag?.id ? String(dag.id) : label,
        });
      }
    }
    if (Array.isArray(block.body)) block.body.forEach(visit);
    if (Array.isArray(block.items)) block.items.flat().forEach(visit);
  };
  blocks.forEach(visit);
  return out;
}

/** hgraph-style mini-map: one 12px square per statement, colored by status,
 * each square clickable to its statement. */
function Squares({ decls, onOpen }: { decls: BlueprintDeclIndexItem[]; onOpen: (d: BlueprintDeclIndexItem) => void }) {
  if (!decls.length) return null;
  return (
    <span className={styles.mmCells}>
      {decls.map((d) => (
        <i
          key={d.label}
          className={styles.mm}
          style={{ background: STATUS_COLOR[d.status] }}
          title={`${d.kind}${d.num ? ` ${d.num}` : ''} · ${d.label} — ${STATUS_LABEL[d.status]}`}
          onClick={(e) => { e.stopPropagation(); onOpen(d); }}
        />
      ))}
    </span>
  );
}

/** hgraph-style segmented progress bar: one proportional segment per status. */
function SegBar({ decls }: { decls: BlueprintDeclIndexItem[] }) {
  const total = decls.length;
  if (!total) return null;
  return (
    <span className={styles.segbar} title={STATUS_ORDER
      .map((s) => `${STATUS_LABEL[s]}: ${decls.filter((d) => d.status === s).length}`)
      .join(' · ')}>
      {STATUS_ORDER.map((s) => {
        const n = decls.filter((d) => d.status === s).length;
        return n > 0
          ? <i key={s} style={{ width: `${(100 * n) / total}%`, background: STATUS_COLOR[s] }} />
          : null;
      })}
    </span>
  );
}

function StatusLegend() {
  return (
    <span className={styles.mmLegend}>
      {STATUS_ORDER.map((s) => (
        <span key={s}><i className={styles.mm} style={{ background: STATUS_COLOR[s] }} /> {STATUS_LABEL[s]}</span>
      ))}
    </span>
  );
}

/**
 * Blueprint — a leanblueprint-quality reading view. The whole blueprint is
 * numbered up front (cheap, no KaTeX) so chapter/section/theorem numbers and
 * cross-references stay global, but only the chapters the user opens are
 * rendered with KaTeX, so big projects load a title page + table of contents
 * fast.
 */
export default function BlueprintPage({ state }: { state: any }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedProject = searchParams.get('project') ?? '';
  const focusLabel = searchParams.get('focus') ?? '';
  const slugParam = searchParams.get('slug') ?? '';
  const anchorParam = searchParams.get('anchor') ?? '';
  const projects: string[] = state.projects ?? [];
  const [project, setProject] = useState(projects[0] ?? '');
  const [data, setData] = useState<BlueprintChaptersResponse | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (requestedProject && projects.includes(requestedProject) && project !== requestedProject) {
      setProject(requestedProject);
    } else if (project && !projects.includes(project)) setProject(projects[0] ?? '');
    else if (!project && projects[0]) setProject(projects[0]);
  }, [projects.join('|'), requestedProject, project]);

  useEffect(() => {
    if (!project) { setData(null); return; }
    setLoading(true);
    getBlueprintChapters(project)
      .then(setData)
      .catch(() => setData({ chapters: [], hasBlueprint: false, error: 'Failed to load the blueprint.' }))
      .finally(() => setLoading(false));
  }, [project]);

  // The full DAG (carrying each node's Lean source, needed for the code chips) is
  // fetched on demand — /api/state now ships only light DAG nodes.
  const [fullDag, setFullDag] = useState<BlueprintDagResponse | null>(null);
  useEffect(() => {
    if (!project) { setFullDag(null); return; }
    let cancelled = false;
    getBlueprintDag(project)
      .then((d) => { if (!cancelled) setFullDag(d); })
      .catch(() => { if (!cancelled) setFullDag(null); });
    return () => { cancelled = true; };
  }, [project]);

  const macros = data?.macros ?? {};
  const chapters = useMemo(() => data?.chapters ?? [], [data]);
  const bibEntries = useMemo(() => data?.bib ?? [], [data]);
  const bib = useMemo(() => bibMapFrom(bibEntries), [bibEntries]);
  const { doc, labels } = useMemo(() => buildBlueprintModel(chapters, true), [chapters]);
  const dagNodes: any[] = useMemo(
    () => fullDag?.nodes ?? state.blueprints?.[project]?.nodes ?? [],
    [fullDag, state.blueprints, project],
  );
  const dagById = useMemo(() => new Map(dagNodes.map((n) => [String(n.id), n])), [dagNodes]);
  const leanSource = useMemo(() => {
    const out = new Map<string, string>();
    for (const n of dagNodes) {
      const name = n.lean_name ?? n.lean;
      if (!name || !n.lean_source) continue;
      out.set(String(name), String(n.lean_source));
    }
    return out;
  }, [dagNodes]);
  const leanTargets = useMemo(() => {
    const out = new Map<string, { file: string; decl: string }>();
    for (const n of dagNodes) {
      const name = n.lean_name ?? n.lean;
      if (!name || !n.lean_file) continue;
      const target = { file: String(n.lean_file), decl: String(name) };
      out.set(String(name), target);
      out.set(String(name).split('.').pop() ?? String(name), target);
    }
    return out;
  }, [dagNodes]);

  const [open, setOpen] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const pending = useRef<string | null>(null);

  useEffect(() => { setOpen(new Set()); setExpanded(new Set()); }, [project]);
  // Scroll to a pending anchor, retrying across frames: the target block may not
  // be painted yet because a freshly-opened chapter streams its blocks in
  // progressively (see ChapterView). Poll for up to ~2s, then give up.
  const scrollToPending = useCallback(() => {
    const id = pending.current;
    if (!id) return;
    const deadline = performance.now() + 2000;
    const tick = () => {
      if (pending.current !== id) return; // superseded by a newer target
      const el = document.getElementById(id);
      if (el) { el.scrollIntoView({ block: 'start' }); pending.current = null; return; }
      if (performance.now() < deadline) requestAnimationFrame(tick);
      else pending.current = null;
    };
    requestAnimationFrame(tick);
  }, []);
  useEffect(() => { scrollToPending(); }, [open, scrollToPending]);

  const openTo = useCallback((slug: string, anchor?: string) => {
    pending.current = anchor ?? `ch-${slug}`;
    if (open.has(slug)) { scrollToPending(); return; }
    setOpen((v) => { const n = new Set(v); n.add(slug); return n; });
  }, [open, scrollToPending]);

  useEffect(() => {
    if (!data?.hasBlueprint) return;
    if (focusLabel) {
      const target = labels.get(focusLabel);
      if (target) { openTo(target.slug, target.anchor); return; }
    }
    if (slugParam) openTo(slugParam, anchorParam || undefined);
  }, [data?.hasBlueprint, focusLabel, slugParam, anchorParam, labels, openTo]);

  const toggleOpen = useCallback((slug: string) => setOpen((v) => { const n = new Set(v); n.has(slug) ? n.delete(slug) : n.add(slug); return n; }), []);
  const toggleExpand = useCallback((slug: string) => setExpanded((v) => { const n = new Set(v); n.has(slug) ? n.delete(slug) : n.add(slug); return n; }), []);
  const openGraph = useCallback((label: string) => {
    navigate(`/dag?project=${encodeURIComponent(project)}&focus=${encodeURIComponent(label)}`);
  }, [navigate, project]);
  const openLean = useCallback((name: string) => {
    const target = leanTargets.get(name) ?? leanTargets.get(name.split('.').pop() ?? name);
    if (!target) return;
    const qs = new URLSearchParams({ project, file: target.file, decl: target.decl });
    navigate(`/lean?${qs.toString()}`);
  }, [leanTargets, navigate, project]);
  const openDecl = useCallback((item: BlueprintDeclIndexItem) => {
    if (item.slug) openTo(item.slug, item.anchor || undefined);
  }, [openTo]);
  const openChapters = doc.filter((c) => open.has(c.slug));
  const declIndex = useMemo(() => (
    openChapters.flatMap((ch) => gatherDecls(ch.blocks as any[], labels as any, dagById))
  ), [openChapters, labels, dagById]);
  // Per-chapter statement index for the mini-map (cheap: walks parsed blocks,
  // no KaTeX) — remark-like environments carry no status, so skip them.
  const chapterDecls = useMemo(() => {
    const out = new Map<string, BlueprintDeclIndexItem[]>();
    for (const ch of doc) {
      out.set(ch.slug, gatherDecls(ch.blocks as any[], labels as any, dagById)
        .filter((d) => !NO_STATUS_KINDS.has(d.envName)));
    }
    return out;
  }, [doc, labels, dagById]);
  const allDecls = useMemo(() => doc.flatMap((ch) => chapterDecls.get(ch.slug) ?? []), [doc, chapterDecls]);
  const declStats = useMemo(() => ({
    leanok: declIndex.filter((d) => d.status === 'leanok').length,
    mathlibok: declIndex.filter((d) => d.status === 'mathlibok').length,
    none: declIndex.filter((d) => d.status === 'none').length,
  }), [declIndex]);

  return (
    <div className="page full-page">
      <div className="panel-heading panel-heading-inline">
        <h2>Blueprint</h2>
        <span className="heading-stat">{data?.docTitle ? <TitleInline tex={data.docTitle} macros={macros} /> : project || 'Select a project'}{doc.length > 0 ? ` · ${doc.length} chapter${doc.length === 1 ? '' : 's'}` : ''}</span>
        <span style={{ marginLeft: 'auto' }}><ProjectPicker projects={projects} value={project} onChange={setProject} /></span>
      </div>

      <div className={styles.body}>
        <aside className={styles.toc}>
          <div className={styles.tocHead}>Contents</div>
          {doc.length === 0 && <div className={styles.tocEmpty}>—</div>}
          {doc.map((ch) => (
            <div key={ch.slug}>
              <div className={styles.tocChapRow}>
                <button className={styles.tocCaret} onClick={() => toggleExpand(ch.slug)} title="Show sections">
                  {ch.sections.length ? (expanded.has(ch.slug) ? '▾' : '▸') : '·'}
                </button>
                <button className={`${styles.tocChap} ${open.has(ch.slug) ? styles.tocActive : ''}`}
                  onClick={() => (open.has(ch.slug) ? toggleOpen(ch.slug) : openTo(ch.slug))}>
                  <span className={styles.tocNum}>{ch.num}</span>
                  <span className={styles.tocTitle}><TitleInline nodes={ch.title} macros={macros} /></span>
                </button>
              </div>
              {expanded.has(ch.slug) && ch.sections.map((s) => (
                <button key={s.anchor} className={styles.tocSec} style={{ paddingLeft: s.level === 2 ? 26 : 38 }}
                  onClick={() => openTo(ch.slug, s.anchor)}>
                  <span className={styles.tocNum}>{s.num}</span>
                  <span className={styles.tocTitle}><TitleInline nodes={s.title} macros={macros} /></span>
                </button>
              ))}
            </div>
          ))}
        </aside>

        <main className={styles.reading}>
          {loading && <div className={styles.muted}>Loading blueprint…</div>}
          {!loading && data && !data.hasBlueprint && (
            <div className={styles.muted}>{data.error ?? 'No blueprint found under blueprint/src/.'}</div>
          )}

          {data?.hasBlueprint && (
            <header className={styles.titlePage}>
              <h1 className={styles.docTitle}>
                {data.docTitle ? <TitleInline tex={data.docTitle} macros={macros} /> : 'Blueprint'}
              </h1>
              {data.docAuthor && <div className={styles.docAuthor}><TitleInline tex={data.docAuthor} macros={macros} /></div>}
              <div className={styles.docMeta}>{doc.length} chapter{doc.length === 1 ? '' : 's'} · {project}</div>
            </header>
          )}

          {openChapters.length === 0 && doc.length > 0 && (
            <nav className={styles.bigToc}>
              {allDecls.length > 0 && (
                <div className={styles.overviewBar}>
                  <SegBar decls={allDecls} />
                  <StatusLegend />
                </div>
              )}
              <div className={styles.bigTocHead}>Table of Contents <span className={styles.bigTocHint}>— click to open a chapter; each square is a statement</span></div>
              {doc.map((ch) => (
                <div key={ch.slug} className={styles.bigTocChap}>
                  <button className={styles.bigTocChapLink} onClick={() => openTo(ch.slug)}>
                    <span className={styles.tocNum}>{ch.num}</span> <TitleInline nodes={ch.title} macros={macros} />
                  </button>
                  <Squares
                    decls={chapterDecls.get(ch.slug) ?? []}
                    onOpen={(d) => openTo(ch.slug, d.anchor || undefined)}
                  />
                  {ch.sections.length > 0 && (
                    <div className={styles.bigTocSecs}>
                      {ch.sections.map((s) => (
                        <button key={s.anchor} className={styles.bigTocSec} style={{ marginLeft: s.level === 3 ? 16 : 0 }}
                          onClick={() => openTo(ch.slug, s.anchor)}>
                          <span className={styles.tocNum}>{s.num}</span> <TitleInline nodes={s.title} macros={macros} />
                        </button>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </nav>
          )}

          {openChapters.map((ch) => (
            <div key={ch.slug} className={styles.openChapter}>
              <ChapterView
                chapter={ch}
                macros={macros}
                labels={labels}
                bib={bib}
                leanSource={leanSource}
                onNavigate={openTo}
                onOpenInGraph={openGraph}
                onOpenInLean={openLean}
              />
            </div>
          ))}

          {data?.hasBlueprint && bibEntries.length > 0 && openChapters.length > 0 && (
            <BibliographyView bib={bibEntries} />
          )}
        </main>

        <aside className={styles.declPanel}>
          <div className={styles.declHead}>
            <span>Open declarations</span>
            <span className={styles.declTotal}>{declIndex.length}</span>
          </div>
          {declIndex.length > 0 && (
            <div className={styles.declStats}>
              <span className={styles.statLean}>leanok {declStats.leanok}</span>
              <span className={styles.statMathlib}>mathlibok {declStats.mathlibok}</span>
              <span className={styles.statNone}>todo {declStats.none}</span>
            </div>
          )}
          <div className={styles.declScroll}>
            {declIndex.length === 0 ? (
              <p className={styles.tocEmpty}>Open a chapter to list its declarations.</p>
            ) : (
              declIndex.map((item) => {
                const leanName = item.leanNames[0];
                return (
                  <div key={item.label} className={`${styles.declRow} ${styles[`declKind_${item.envName}`] ?? ''}`}>
                    <button className={styles.declJump} onClick={() => openDecl(item)} title={`Open ${item.label}`}>
                      <span className={styles.declKind}>{item.kind}{item.num ? ` ${item.num}` : ''}</span>
                      <span className={styles.declTitle}>
                        {item.title ? <TitleInline tex={item.title} macros={macros} /> : item.label}
                      </span>
                      <span className={styles.declLabel}>{item.label}</span>
                    </button>
                    <span className={styles.declActions}>
                      <span className={`${styles.declStatus} ${styles[`status_${item.status}`]}`}>{STATUS_LABEL[item.status]}</span>
                      {leanName && (() => {
                        const resolvable = leanTargets.has(leanName) || leanTargets.has(leanName.split('.').pop() ?? leanName);
                        return (
                          <button
                            onClick={() => openLean(leanName)}
                            disabled={!resolvable}
                            title={resolvable
                              ? 'Open matching Lean declaration'
                              : 'No Lean source linked yet — run `horizon blueprint` to rebuild the DAG and resolve \\lean{} targets to their file'}
                          >lean</button>
                        );
                      })()}
                      {item.graphId && <button onClick={() => openGraph(item.graphId!)} title="Show matching DAG node">graph</button>}
                    </span>
                  </div>
                );
              })
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
