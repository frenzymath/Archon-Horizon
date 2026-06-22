import React, { useEffect, useState } from 'react';
import { editInbox, getState, getTranscript, getTranscripts } from './api';
import { isStaticDashboard } from './staticMode';

const STATIC = isStaticDashboard();

export function App() {
  const [state, setState] = useState<any>(null);
  const [tab, setTab] = useState<'overview' | 'blueprint' | 'transcripts'>('overview');

  const reload = () => getState().then(setState).catch(() => {});

  useEffect(() => {
    reload();
    if (STATIC) return; // static export is a snapshot; nothing to poll
    const id = setInterval(reload, 5000);
    return () => clearInterval(id);
  }, []);

  if (!state) return <div className="wrap">Loading…</div>;

  return (
    <div className="wrap">
      <header>
        <h1>{state.workspace}</h1>
        <span className={`mode ${STATIC ? 'static' : 'live'}`}>
          {STATIC ? 'static · read-only' : 'live'}
        </span>
      </header>
      <nav>
        <button className={tab === 'overview' ? 'on' : ''} onClick={() => setTab('overview')}>
          Overview
        </button>
        <button className={tab === 'blueprint' ? 'on' : ''} onClick={() => setTab('blueprint')}>
          Blueprint
        </button>
        <button className={tab === 'transcripts' ? 'on' : ''} onClick={() => setTab('transcripts')}>
          Transcripts
        </button>
      </nav>
      {tab === 'overview' && <Overview state={state} reload={reload} />}
      {tab === 'blueprint' && <Blueprint blueprints={state.blueprints ?? {}} />}
      {tab === 'transcripts' && <Transcripts />}
    </div>
  );
}

function Blueprint({ blueprints }: { blueprints: Record<string, any> }) {
  const projects = Object.keys(blueprints);
  if (projects.length === 0) return <section><p><em>no parseable blueprints</em></p></section>;
  return (
    <section>
      {projects.map((p) => {
        const dag = blueprints[p];
        return (
          <div key={p}>
            <h2>{p}</h2>
            <p className="note">
              {dag.nodes.length} nodes · {dag.edges.length} edges · {dag.dangling.length} dangling
            </p>
            <ul>
              {dag.nodes.map((n: any) => (
                <li key={n.id}>
                  <strong>{n.id}</strong> ({n.kind}){n.leanok ? ' ✓' : ''}
                  {(dag.edges.filter((e: any) => e.target === n.id).length > 0) && (
                    <span className="deps"> ← {dag.edges.filter((e: any) => e.target === n.id).map((e: any) => e.source).join(', ')}</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}

function Overview({ state, reload }: { state: any; reload: () => void }) {
  const items = state.roadmap?.items ?? [];
  return (
    <section>
      <h2>Roadmap</h2>
      <table>
        <thead>
          <tr><th>ID</th><th>Title</th><th>Status</th><th>Projects</th></tr>
        </thead>
        <tbody>
          {items.map((i: any) => (
            <tr key={i.id}>
              <td>{i.id}</td><td>{i.title}</td><td>{i.status}</td><td>{(i.projects || []).join(', ')}</td>
            </tr>
          ))}
          {items.length === 0 && <tr><td colSpan={4}><em>empty</em></td></tr>}
        </tbody>
      </table>

      <h2>Tasks</h2>
      <ul>
        {(state.tasks ?? []).map((t: any) => (
          <li key={t.id}>{t.id} [{t.status}] — {t.objective}</li>
        ))}
        {(state.tasks ?? []).length === 0 && <li><em>none</em></li>}
      </ul>

      <h2>Local inbox</h2>
      <InboxList items={state.local_inbox ?? []} reload={reload} />

      <h2>Memory</h2>
      <pre>{state.memory || '(empty)'}</pre>
    </section>
  );
}

function InboxList({ items, reload }: { items: any[]; reload: () => void }) {
  const [body, setBody] = useState('');
  return (
    <div>
      <ul>
        {items.map((i: any) => (
          <li key={i.id}>
            <strong>{i.id}</strong> ({i.kind}, {i.status}) {i.body}
            {!STATIC && i.status === 'open' && (
              <button className="mini" onClick={() => editInbox({ action: 'complete', id: i.id }).then(reload)}>
                ✓ done
              </button>
            )}
          </li>
        ))}
        {items.length === 0 && <li><em>empty</em></li>}
      </ul>
      {!STATIC && (
        <div className="add">
          <input value={body} placeholder="new hint…" onChange={(e) => setBody(e.target.value)} />
          <button
            onClick={() => {
              if (body) editInbox({ action: 'add', kind: 'hint', body }).then(() => { setBody(''); reload(); });
            }}
          >
            Add hint
          </button>
        </div>
      )}
    </div>
  );
}

function Transcripts() {
  const [list, setList] = useState<any[]>([]);
  const [events, setEvents] = useState<any[] | null>(null);

  useEffect(() => {
    getTranscripts().then(setList).catch(() => setList([]));
  }, []);

  return (
    <section className="cols">
      <div className="list">
        <h2>Sessions</h2>
        {list.length === 0 && <p><em>no transcripts yet</em></p>}
        <ul>
          {list.map((t: any, idx: number) => (
            <li key={idx}>
              <button onClick={() => getTranscript(t.ref).then(setEvents)}>
                {t.run}/{t.parent ? `${t.parent}/` : ''}{t.session}
              </button>
            </li>
          ))}
        </ul>
      </div>
      <div className="viewer">
        <h2>Transcript</h2>
        {events === null && <p><em>select a session</em></p>}
        {events?.map((e: any, i: number) => (
          <div key={i} className={`ev ${e.kind}`}>
            <span className="k">
              {e.kind}{e.tool ? ` · ${e.tool}` : ''}{formatUsage(e.usage)}
            </span>
            {e.text && <pre>{e.text}</pre>}
          </div>
        ))}
      </div>
    </section>
  );
}

function formatUsage(usage: any) {
  if (!usage) return '';
  const cached = usage.cached_tokens_in ? ` (${usage.cached_tokens_in} cached)` : '';
  const reasoning = usage.reasoning_tokens_out ? ` (${usage.reasoning_tokens_out} reasoning)` : '';
  const cost = typeof usage.cost_usd === 'number' ? ` / $${usage.cost_usd.toFixed(4)}` : '';
  return ` · ${usage.tokens_in ?? 0} in${cached} / ${usage.tokens_out ?? 0} out${reasoning}${cost}`;
}
