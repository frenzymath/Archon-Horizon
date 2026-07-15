import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { getSourceFiles, getSourceFile, type SourceFile } from './api';
import LeanCodeLine from './components/LeanCodeLine';
import ProjectPicker from './components/ProjectPicker';
import { useProgressiveCount } from './hooks/useProgressiveCount';
import { highlightLeanLines } from './utils/leanHighlight';
import { scanLinesForSorry } from './utils/sorryScanner';
import { extractLeanStructureFromLines, groupStructureCounts } from './utils/leanStructure';

type TreeNode = { name: string; path: string; children: TreeNode[]; file?: SourceFile };

function buildTree(files: SourceFile[]): TreeNode[] {
  const root: TreeNode = { name: '', path: '', children: [] };
  for (const file of files) {
    const parts = file.path.split('/').filter(Boolean);
    let current = root;
    let prefix = '';
    parts.forEach((part, idx) => {
      prefix = prefix ? `${prefix}/${part}` : part;
      let child = current.children.find((c) => c.name === part);
      if (!child) { child = { name: part, path: prefix, children: [] }; current.children.push(child); }
      if (idx === parts.length - 1) child.file = file;
      current = child;
    });
  }
  // Collapse single-child directory chains (a/b/c -> a/b/c) for compactness.
  const collapse = (node: TreeNode) => {
    while (node.children.length === 1 && !node.children[0].file && !node.file) {
      const only = node.children[0];
      node.name = node.name ? `${node.name}/${only.name}` : only.name;
      node.path = only.path;
      node.children = only.children;
    }
    node.children.forEach(collapse);
  };
  const sort = (nodes: TreeNode[]) => {
    nodes.sort((a, b) => {
      const aDir = !a.file, bDir = !b.file;
      if (aDir !== bDir) return aDir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    nodes.forEach((n) => sort(n.children));
  };
  root.children.forEach(collapse);
  sort(root.children);
  return root.children;
}

function FileTree({ nodes, selected, onSelect }: { nodes: TreeNode[]; selected: string; onSelect: (p: string) => void }) {
  return (
    <div className="lean-tree">
      {nodes.map((node) =>
        node.file ? (
          (() => {
            const s = node.file.sorries ?? 0;
            return (
              <button key={node.path} className={`lean-file-row ${selected === node.file.path ? 'active' : ''} ${s > 0 ? 'has-sorry' : 'no-sorry'}`} onClick={() => onSelect(node.file!.path)} title={`${node.file.path}${s > 0 ? ` — ${s} sorry` : ' — no sorry'}`}>
                <span className="lean-file-dot" aria-hidden>{s > 0 ? '●' : '✓'}</span>
                <span className="lean-file-name">{node.name}</span>
                {s > 0 && <span className="lean-file-sorry">{s}</span>}
              </button>
            );
          })()
        ) : (
          <details key={node.path} open>
            <summary>{node.name}/</summary>
            <div className="lean-tree-children">
              <FileTree nodes={node.children} selected={selected} onSelect={onSelect} />
            </div>
          </details>
        ),
      )}
    </div>
  );
}

/** Strip Lean comments while preserving the line count (so line numbers and the
 *  outline still line up). Handles `--` line comments and nested `/- -/`. */
function stripComments(src: string): string {
  let depth = 0;
  return src.split('\n').map((line) => {
    let out = '';
    let i = 0;
    while (i < line.length) {
      if (depth > 0) {
        if (line[i] === '/' && line[i + 1] === '-') { depth++; i += 2; continue; }
        if (line[i] === '-' && line[i + 1] === '/') { depth--; i += 2; continue; }
        i++; continue;
      }
      if (line[i] === '-' && line[i + 1] === '-') break;
      if (line[i] === '/' && line[i + 1] === '-') { depth++; i += 2; continue; }
      out += line[i]; i++;
    }
    return out.replace(/\s+$/, '');
  }).join('\n');
}

const KIND_ORDER = ['theorem', 'lemma', 'def', 'instance', 'structure', 'class', 'inductive', 'abbrev', 'example', 'sorry'];

// Memoized so streaming the file in progressively (see useProgressiveCount)
// doesn't re-render the lines already on screen — token arrays keep a stable
// identity, so growing the visible count stays O(n) overall.
const LeanSourceRow = React.memo(function LeanSourceRow({
  n, text, tokens, isSorry, flash,
}: { n: number; text: string; tokens: any; isSorry: boolean; flash: boolean }) {
  return (
    <div id={`lean-line-${n}`} className={`lean-source-line${isSorry ? ' has-sorry' : ''}${flash ? ' flash' : ''}`}>
      <span className="lean-gutter">{n}</span>
      <span className="lean-line-text"><LeanCodeLine text={text} tokens={tokens} /></span>
    </div>
  );
});

export default function LeanPage({ state }: { state: any }) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const requestedProject = searchParams.get('project') ?? '';
  const requestedFile = searchParams.get('file') ?? '';
  const requestedDecl = searchParams.get('decl') ?? '';
  const projects: string[] = state.projects ?? [];
  const [project, setProject] = useState(projects[0] ?? '');
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [selected, setSelected] = useState<string>('');
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [stripped, setStripped] = useState(false);
  const [flash, setFlash] = useState<number | null>(null);

  useEffect(() => {
    if (requestedProject && projects.includes(requestedProject) && project !== requestedProject) {
      setProject(requestedProject);
    } else if (project && !projects.includes(project)) setProject(projects[0] ?? '');
    else if (!project && projects[0]) setProject(projects[0]);
  }, [projects.join('|'), requestedProject, project]);

  useEffect(() => {
    if (!project) { setFiles([]); return; }
    setSelected(requestedFile); setContent(null);
    getSourceFiles(project).then((r) => setFiles(r.files ?? [])).catch(() => setFiles([]));
  }, [project, requestedFile]);

  useEffect(() => {
    if (!project || !selected) { setContent(null); return; }
    setLoading(true);
    getSourceFile(project, selected)
      .then((r) => setContent(r.content))
      .catch(() => setContent('-- failed to load file'))
      .finally(() => setLoading(false));
  }, [project, selected]);

  const tree = useMemo(() => buildTree(files), [files]);
  const rawLines = useMemo(() => (content ?? '').split('\n'), [content]);
  const strippedLines = useMemo(() => stripComments(content ?? '').split('\n'), [content]);
  // Rows carry their ORIGINAL line number so the gutter, outline jumps and
  // sorry highlighting stay correct even when comments are condensed out.
  const rows = useMemo(() => {
    if (!stripped) return rawLines.map((text, i) => ({ n: i + 1, text }));
    // Drop comment lines but keep a single blank line wherever there were blanks
    // (so declarations stay visually separated).
    const out: { n: number; text: string }[] = [];
    let prevBlank = false;
    strippedLines.forEach((text, i) => {
      if (text.trim() === '') {
        if (!prevBlank && out.length) out.push({ n: i + 1, text: '' });
        prevBlank = true;
      } else {
        out.push({ n: i + 1, text });
        prevBlank = false;
      }
    });
    while (out.length && out[out.length - 1].text === '') out.pop();
    return out;
  }, [stripped, rawLines, strippedLines]);
  const highlighted = useMemo(() => highlightLeanLines(rows.map((r) => r.text)), [rows]);
  const sorryLines = useMemo(() => scanLinesForSorry(rawLines), [rawLines]);
  const outline = useMemo(() => extractLeanStructureFromLines(rawLines), [rawLines]);
  const declTargets = useMemo(() => {
    const nodes: any[] = state.blueprints?.[project]?.nodes ?? [];
    const out = new Map<string, { id: string }>();
    const add = (key: string | null | undefined, id: string) => {
      if (!key) return;
      if (!out.has(key)) out.set(key, { id });
    };
    const selectedNodes = nodes.filter((n) => !n.lean_file || !selected || n.lean_file === selected);
    for (const n of selectedNodes) {
      const id = n.id ? String(n.id) : '';
      const name = n.lean_name ?? n.lean;
      if (!id || !name) continue;
      const full = String(name);
      add(full, id);
      add(full.split('.').pop(), id);
    }
    return out;
  }, [state.blueprints, project, selected]);
  const counts = useMemo(() => groupStructureCounts(outline), [outline]);
  // Progressive rendering: show the first lines immediately and stream the rest
  // in over the next frames, so a large file's syntax-highlighted view doesn't
  // block until every line is built. Reset when the file / strip toggle changes.
  const shownRows = useProgressiveCount(rows.length, {
    resetKey: `${project}|${selected}|${stripped}`,
    initial: 200,
    step: 400,
  });
  const rowsRemaining = rows.length - shownRows;
  const loc = content === null ? 0 : rawLines.length;
  const locCode = useMemo(() => strippedLines.filter((l) => l.trim() !== '').length, [strippedLines]);
  const totals = useMemo(() => ({
    loc: files.reduce((a, f) => a + (f.loc ?? 0), 0),
    code: files.reduce((a, f) => a + (f.loc_code ?? 0), 0),
    sorries: files.reduce((a, f) => a + (f.sorries ?? 0), 0),
  }), [files]);
  const fmt = (n: number) => n.toLocaleString();

  const jumpTo = (line: number) => {
    const el = document.getElementById(`lean-line-${line}`);
    if (el) el.scrollIntoView({ block: 'center' });
    setFlash(line);
    window.setTimeout(() => setFlash((f) => (f === line ? null : f)), 1200);
  };

  const jumpedDecl = useRef<string>('');
  useEffect(() => { jumpedDecl.current = ''; }, [requestedDecl, selected]);
  useEffect(() => {
    if (!requestedDecl || !selected || content === null) return;
    if (jumpedDecl.current === requestedDecl) return;
    const short = requestedDecl.split('.').pop();
    const match = outline.find((item) => item.label === requestedDecl || item.label === short || requestedDecl.endsWith(`.${item.label}`));
    if (!match) return;
    // The target line may not be painted yet while the file streams in
    // progressively; keep waiting (this effect re-runs as shownRows grows) until
    // its row exists, then jump exactly once.
    if (!document.getElementById(`lean-line-${match.line}`)) return;
    jumpedDecl.current = requestedDecl;
    const tid = window.setTimeout(() => jumpTo(match.line), 0);
    return () => window.clearTimeout(tid);
  }, [requestedDecl, selected, content, outline, shownRows]);

  const openBlueprint = (nodeId: string) => {
    navigate(`/blueprint?project=${encodeURIComponent(project)}&focus=${encodeURIComponent(nodeId)}`);
  };
  const openGraph = (nodeId: string) => {
    navigate(`/dag?project=${encodeURIComponent(project)}&focus=${encodeURIComponent(nodeId)}`);
  };

  return (
    <div className="page full-page">
      <div className="panel-heading panel-heading-inline">
        <h2>Lean</h2>
        <span className="heading-stat">{project
          ? `${files.length} Lean file${files.length === 1 ? '' : 's'} · ${fmt(totals.loc)} loc · ${fmt(totals.code)} code${totals.sorries ? ` · ${totals.sorries} sorry` : ''}`
          : 'Select a project'}</span>
        <span style={{ marginLeft: 'auto' }}><ProjectPicker projects={projects} value={project} onChange={setProject} /></span>
      </div>

      {projects.length === 0 ? (
        <p className="empty">No projects found.</p>
      ) : (
        <>
          <div className="lean-layout">
            {/* Files */}
            <div className="lean-pane">
              <div className="lean-pane-head">Files</div>
              <div className="lean-pane-scroll">
                {files.length === 0 ? <p className="empty">No Lean files.</p> : <FileTree nodes={tree} selected={selected} onSelect={setSelected} />}
              </div>
            </div>

            {/* Source */}
            <div className="lean-pane lean-code-pane">
              <div className="lean-pane-head">
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{selected || '(no file selected)'}</span>
                {selected && (
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 10, textTransform: 'none', fontWeight: 500 }}>
                    <span title="lines of code (total · excluding comments & blanks)">{loc} loc · {locCode} code</span>
                    <label style={{ display: 'inline-flex', alignItems: 'center', gap: 4, cursor: 'pointer' }}>
                      <input type="checkbox" checked={stripped} onChange={(e) => setStripped(e.target.checked)} /> strip comments
                    </label>
                  </span>
                )}
              </div>
              <div className="lean-pane-scroll">
                {!selected ? (
                  <p className="empty" style={{ padding: '1rem' }}>Select a file to view its Lean source.</p>
                ) : loading ? (
                  <p className="empty" style={{ padding: '1rem' }}>Loading…</p>
                ) : (
                  <pre className="lean-source">
                    <code>
                      {rows.slice(0, shownRows).map((row, i) => (
                        <LeanSourceRow
                          key={row.n}
                          n={row.n}
                          text={row.text}
                          tokens={highlighted[i]}
                          isSorry={sorryLines.has(row.n)}
                          flash={flash === row.n}
                        />
                      ))}
                      {rowsRemaining > 0 && (
                        <div className="lean-source-line">
                          <span className="lean-gutter" />
                          <span className="lean-line-text" style={{ opacity: 0.55 }}>… rendering {rowsRemaining} more line{rowsRemaining === 1 ? '' : 's'}</span>
                        </div>
                      )}
                    </code>
                  </pre>
                )}
              </div>
            </div>

            {/* Outline */}
            <div className="lean-pane">
              <div className="lean-pane-head">Declarations</div>
              {selected && outline.length > 0 && (
                <div className="lean-outline-counts">
                  {KIND_ORDER.filter((k) => counts[k]).map((k) => (
                    <span key={k} className={`lean-count${k === 'sorry' ? ' sorry' : ''}`}>{k} {counts[k]}</span>
                  ))}
                </div>
              )}
              <div className="lean-pane-scroll">
                {!selected ? (
                  <p className="empty">—</p>
                ) : outline.length === 0 ? (
                  <p className="empty">No declarations.</p>
                ) : (
                  outline.map((item) => {
                    const target = declTargets.get(item.label);
                    return (
                      <div key={item.id} className={`lean-outline-row${item.kind === 'sorry' ? ' sorry' : ''}`}>
                        <button className="lean-outline-jump" onClick={() => jumpTo(item.line)} title={`line ${item.line}`}>
                          <span className="lean-outline-kind">{item.kind}</span>
                          <span className="lean-outline-label">{item.label}</span>
                        </button>
                        {target && (
                          <span className="lean-outline-actions">
                            <button onClick={() => openBlueprint(target.id)} title="Open matching blueprint declaration">bp</button>
                            <button onClick={() => openGraph(target.id)} title="Show matching DAG node">graph</button>
                          </span>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
