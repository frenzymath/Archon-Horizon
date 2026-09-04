import React, { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  getBenchmark,
  getProjects,
  type BenchmarkFile,
  type BenchmarkResponse,
  type ProjectStat,
} from './api';
import { isStaticDashboard } from './staticMode';

const STATIC = isStaticDashboard();
const ALL = '';

function formatHb(n: number): string {
  return n.toLocaleString();
}

function heatClass(heartbeats: number, max: number): string {
  if (max <= 0 || heartbeats <= 0) return '';
  const t = heartbeats / max;
  if (t >= 0.66) return 'bench-hot';
  if (t >= 0.33) return 'bench-warm';
  return 'bench-cool';
}

export default function BenchmarkPage({ state }: { state: { projects?: string[] } }) {
  const [params, setParams] = useSearchParams();
  const [project, setProject] = useState(params.get('project') || ALL);
  const [projects, setProjects] = useState<string[]>(state.projects ?? []);
  const [data, setData] = useState<BenchmarkResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [details, setDetails] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    if (state.projects?.length) {
      setProjects(state.projects);
      return;
    }
    getProjects()
      .then((r) => setProjects((r.projects || []).map((p: ProjectStat) => p.name)))
      .catch(() => undefined);
  }, [state.projects]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const opts = {
      project: project || undefined,
      minHeartbeats: 1,
      details,
    };
    getBenchmark(opts)
      .then((payload) => {
        if (!cancelled) {
          setData(payload);
          setLoading(false);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message || String(err));
          setData(null);
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [project, details]);

  const maxHb = useMemo(
    () => Math.max(0, ...(data?.files || []).map((f) => f.heartbeats)),
    [data],
  );

  const onProject = (next: string) => {
    setProject(next);
    const sp = new URLSearchParams(params);
    if (!next) sp.delete('project');
    else sp.set('project', next);
    setParams(sp, { replace: true });
  };

  return (
    <div className="page benchmark-page">
      <div className="panel-heading panel-heading-inline">
        <h2>Benchmark</h2>
        <span className="heading-stat">
          Lean files by Σ set_option heartbeat budgets
        </span>
        <span className="benchmark-toolbar" style={{ marginLeft: 'auto' }}>
          <label className="project-picker-select">
            <span>Project</span>
            <select
              value={project}
              onChange={(e) => onProject(e.target.value)}
              aria-label="Project"
            >
              <option value="">(all projects)</option>
              {projects.map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </label>
          <label className="benchmark-details-toggle">
            <input
              type="checkbox"
              checked={details}
              onChange={(e) => setDetails(e.target.checked)}
            />
            Line details
          </label>
        </span>
      </div>
      <p className="muted" style={{ marginTop: 0 }}>
        Indicator: sum of <code>set_option maxHeartbeats</code>,{' '}
        <code>synthInstance.maxHeartbeats</code>, and related resource options.
        High ranks often mark layered API debt — CLI: <code>horizon benchmark</code>;
        rewrite guidance: <code>restart-module</code> skill.
      </p>

      {STATIC && (
        <div className="notice info">
          Static snapshot — numbers reflect the export time, not a live scan.
        </div>
      )}

      {error && <div className="notice error">{error}</div>}
      {loading && !data && <p className="empty">Scanning Lean files…</p>}

      {data && (
        <>
          <div className="benchmark-summary">
            <div className="benchmark-stat">
              <span className="benchmark-stat-value">{data.total_files}</span>
              <span className="benchmark-stat-label">files with overrides</span>
            </div>
            <div className="benchmark-stat">
              <span className="benchmark-stat-value">{formatHb(data.total_heartbeats)}</span>
              <span className="benchmark-stat-label">Σ heartbeats</span>
            </div>
            <div className="benchmark-stat">
              <span className="benchmark-stat-value">{data.total_hits}</span>
              <span className="benchmark-stat-label">set_option hits</span>
            </div>
          </div>

          {data.files.length === 0 ? (
            <p className="empty">No set_option heartbeat overrides in scope.</p>
          ) : (
            <div className="panel">
              <div className="panel-header">
                <h3>Files by heartbeat budget</h3>
                <span className="muted">{data.indicator}</span>
              </div>
              <div className="table-wrap">
                <table className="benchmark-table">
                  <thead>
                    <tr>
                      <th className="num">#</th>
                      <th className="num">Heartbeats</th>
                      <th className="num">Hits</th>
                      <th>Project</th>
                      <th>Path</th>
                      <th>Options</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.files.map((row: BenchmarkFile, i: number) => {
                      const key = `${row.project}:${row.path}`;
                      const open = expanded === key;
                      return (
                        <React.Fragment key={key}>
                          <tr
                            className={heatClass(row.heartbeats, maxHb)}
                            onClick={() =>
                              details
                                ? setExpanded(open ? null : key)
                                : undefined
                            }
                            style={details ? { cursor: 'pointer' } : undefined}
                          >
                            <td className="num">{i + 1}</td>
                            <td className="num mono">{formatHb(row.heartbeats)}</td>
                            <td className="num">{row.hits}</td>
                            <td>{row.project}</td>
                            <td className="mono">
                              <Link
                                to={`/lean?project=${encodeURIComponent(row.project)}&file=${encodeURIComponent(row.path)}`}
                                onClick={(e) => e.stopPropagation()}
                              >
                                {row.path}
                              </Link>
                            </td>
                            <td className="muted">
                              {(row.options || []).join(', ') || '—'}
                            </td>
                          </tr>
                          {open && row.details && row.details.length > 0 && (
                            <tr className="benchmark-detail-row">
                              <td colSpan={6}>
                                <ul className="benchmark-hits">
                                  {row.details.map((hit, j) => (
                                    <li key={j}>
                                      <span className="mono">L{hit.line}</span>{' '}
                                      <code>{hit.option}</code> ={' '}
                                      {formatHb(hit.value)}
                                    </li>
                                  ))}
                                </ul>
                              </td>
                            </tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {data.projects.length > 1 && (
            <div className="panel" style={{ marginTop: 16 }}>
              <div className="panel-header">
                <h3>Per project</h3>
              </div>
              <div className="table-wrap">
                <table className="benchmark-table">
                  <thead>
                    <tr>
                      <th>Project</th>
                      <th className="num">Files</th>
                      <th className="num">Σ heartbeats</th>
                      <th className="num">Hits</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...data.projects]
                      .sort((a, b) => b.heartbeats - a.heartbeats)
                      .map((p) => (
                        <tr key={p.project}>
                          <td>
                            <button
                              type="button"
                              className="linkish"
                              onClick={() => onProject(p.project)}
                            >
                              {p.project}
                            </button>
                          </td>
                          <td className="num">{p.files}</td>
                          <td className="num mono">{formatHb(p.heartbeats)}</td>
                          <td className="num">{p.hits}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
