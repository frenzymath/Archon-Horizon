import { useEffect, useRef, useState, type FormEvent } from "react";
import { BookOpen, Library, Search } from "lucide-react";
import { params, request, type Project } from "./api";
import { usePagedRead } from "./queries";
import "../components/search.css";
import "../components/agents.css";

type Repository = {id: string; slug: string; purpose: string; default_branch: string};
type Result = {status: string; indexed_commit: string | null; error?: string; items: {name: string; kind: string; signature: string; doc?: string; header?: string; file: string; line: number}[]};

export default function DesktopSearch({accountId, projectId, projects}: {accountId: string; projectId: string; projects: Project[]}) {
  const [project, setProject] = useState(projectId || projects[0]?.id || "");
  const [scope, setScope] = useState("workspace");
  const [repositoryId, setRepositoryId] = useState("");
  const [mode, setMode] = useState("text");
  const [query, setQuery] = useState("");
  const [subdir, setSubdir] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const repositories = usePagedRead<Repository>(accountId, "repositories", project, `/records/repository?${params({project_id: project, limit: 100})}`, !!project);
  const items = (repositories.data?.pages.flatMap(page => page.items) || []).filter(item => scope === "workspace" ? item.purpose === "workspace" : ["library", "reference"].includes(item.purpose));
  const selected = items.find(item => item.id === repositoryId) || items[0];
  useEffect(() => {if (!project && projects.length) setProject(projects[0].id);}, [project, projects]);
  useEffect(() => {controller.current?.abort(); setResult(null); setError(""); setBusy(false);}, [project, selected?.id]);
  useEffect(() => () => controller.current?.abort(), []);
  const search = async (event: FormEvent) => {
    event.preventDefault();
    if (!selected || !query.trim()) return;
    controller.current?.abort();
    const current = new AbortController(); controller.current = current;
    setBusy(true); setError("");
    try {
      const head = await request<{commit_oid: string}>(`/repositories/${encodeURIComponent(selected.id)}/head`, {signal: current.signal});
      for (let attempt = 0; attempt < 15; attempt++) {
        const value = await request<Result>(`/search?${params({repository_id: selected.id, commit: head.commit_oid, q: query.trim(), mode, subdir: subdir.trim(), limit: 50})}`, {signal: current.signal});
        if (current.signal.aborted) return;
        setResult(value);
        if (value.status !== "preparing") break;
        await new Promise<void>((resolve, reject) => {
          const abort = () => {clearTimeout(timer); reject(new DOMException("Aborted", "AbortError"));};
          const timer = window.setTimeout(() => {current.signal.removeEventListener("abort", abort); resolve();}, 2000);
          current.signal.addEventListener("abort", abort, {once: true});
        });
      }
    } catch (failure) {if (!current.signal.aborted) setError(failure instanceof Error ? failure.message : String(failure));}
    finally {if (!current.signal.aborted) setBusy(false);}
  };
  return <section className="horizon-search"><div className="capability-tabs" role="tablist" aria-label="Search scope">{[["workspace", "Project Lean"], ["libraries", "Libraries"]].map(([value, label]) => <button key={value} role="tab" aria-selected={scope === value} className={scope === value ? "active" : ""} onClick={() => {setScope(value); setRepositoryId("");}}>{value === "workspace" ? <BookOpen size={16}/> : <Library size={16}/>} {label}</button>)}</div>
    <form onSubmit={event => void search(event)} className="horizon-search-toolbar"><div className="horizon-search-selectors"><label>Project<select aria-label="Search project" value={project} onChange={event => setProject(event.target.value)}>{projects.map(item => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label><label>{scope === "workspace" ? "Repository" : "Library"}<select aria-label="Search repository" value={selected?.id || ""} onChange={event => setRepositoryId(event.target.value)}>{!items.length && <option value="">No repositories</option>}{items.map(item => <option key={item.id} value={item.id}>{item.slug}</option>)}</select></label><label>Mode<select aria-label="Search mode" value={mode} onChange={event => setMode(event.target.value)}>{["text", "name", "type", "header"].map(value => <option key={value} value={value}>{value[0].toUpperCase() + value.slice(1)}</option>)}</select></label><label className="horizon-search-lib">Nested project<input aria-label="Nested project filter" value={subdir} onChange={event => setSubdir(event.target.value)} placeholder="All nested projects"/></label></div><div className="horizon-search-query"><label className="platform-search-field"><Search size={17}/><input type="search" aria-label="Lean search query" placeholder="Search Lean declarations" value={query} onChange={event => setQuery(event.target.value)}/></label><button className="platform-primary" disabled={busy || !selected || !query.trim()}><Search size={15}/>{busy ? "Searching..." : "Search"}</button></div></form>
    {(error || repositories.error) && <div className="platform-error" role="alert">{error || repositories.error?.message}</div>}
    {repositories.hasNextPage && <button className="platform-small-button" onClick={() => void repositories.fetchNextPage()}>More repositories</button>}
    {result && <div className="horizon-search-results"><div className="platform-directory-label"><span>{result.status === "ready" ? `${result.items.length} matches` : result.status === "preparing" ? "Indexing published Lean..." : "Index unavailable"}</span><span>{selected?.default_branch}</span></div>{result.error && <div className="platform-error">{result.error}</div>}{result.items.map((hit, index) => <article className="horizon-search-hit" key={`${hit.file}:${hit.line}:${index}`}><header><strong>{hit.name || "(anonymous)"}</strong><span>{hit.kind}</span></header>{hit.signature && <code>{hit.signature}</code>}{(hit.doc || hit.header) && <p>{hit.doc || hit.header}</p>}<small>{hit.file}:{hit.line}</small></article>)}{result.status === "ready" && !result.items.length && <div className="platform-empty">No matches</div>}</div>}
  </section>;
}
