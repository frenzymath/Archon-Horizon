import { useState } from "react";
import { Bot, FileText, Folder, RefreshCw, Search } from "lucide-react";
import { useRead } from "./queries";
import { ErrorNotice, RichText } from "./shared";
import "./InstructionCatalog.css";

type Skill = {name: string; description: string; category: string; path: string; resources: string[]};
type Subagent = {slug: string; description: string; skills: string[]; source_path: string; category: string};
type Catalog = {revision: string; skills: Skill[]; subagents: Subagent[]; files: string[]};
type Entry = {name: string; description: string; path: string; resources: string[]; category: string; skills?: string[]};
const categories = {operations: "Horizon operations", lean: "Lean", review: "Review procedures", custom: "Project skills", implementation: "Implementation", research: "Research", validation: "Validation", planning: "Planning", reviewers: "Reviewers"};

export default function InstructionCatalog({accountId, mode}: {accountId: string; mode: "skills" | "descriptors"}) {
  const catalog = useRead<Catalog>(accountId, "instructions", "global", "/instruction-catalog");
  const [search, setSearch] = useState("");
  const [selection, setSelection] = useState("");
  const [resource, setResource] = useState("");
  const [view, setView] = useState<"preview" | "source">("preview");
  const entries: Entry[] = mode === "skills" ? catalog.data?.skills || [] : (catalog.data?.subagents || []).map(item => ({
    name: item.slug, description: item.description, path: item.source_path, skills: item.skills, resources: [], category: item.category,
  }));
  const filtered = entries.filter(item => `${item.name} ${item.description} ${item.category}`.toLowerCase().includes(search.toLowerCase()));
  const active = filtered.find(item => item.path === selection) || filtered[0];
  const path = active && (catalog.data?.files.includes(resource) ? resource : active.path);
  const file = useRead<{path: string; content: string; instructions?: string; sha256: string}>(accountId, "instructions", "global",
    `/instruction-catalog/file?path=${encodeURIComponent(path || "")}`, !!path);
  const content = file.data?.instructions || file.data?.content.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n/, "") || "";
  return <>
    <div className="capability-toolbar">
      <label className="capability-search"><Search size={15} /><input type="search" aria-label="Search installed instructions" placeholder="Search instructions" value={search} onChange={event => setSearch(event.target.value)} /></label>
      <span className="capability-library-location">{entries.length} {mode === "skills" ? "skills" : "subagent descriptors"} / Installed catalog</span>
      <button type="button" className="platform-icon-button" title="Refresh installed catalog" aria-label="Refresh installed catalog" onClick={() => {void catalog.refetch(); if (path) void file.refetch();}}><RefreshCw size={15} /></button>
    </div>
    <ErrorNotice error={catalog.error} stale={!!catalog.data} />
    <div className="capability-workspace instruction-catalog">
      <aside className="capability-directory" aria-label="Installed instruction folders">
        {Array.from(new Set(entries.map(item => item.category))).sort((a,b) => {
          const order = Object.keys(categories);
          return order.indexOf(a) - order.indexOf(b);
        }).map(category => {
          const title = categories[category as keyof typeof categories] || category;
          const group = filtered.filter(item => item.category === category);
          return group.length ? <details key={category} open className="instruction-folder"><summary><Folder size={14}/>{title}<small>{group.length}</small></summary><ul>
            {group.map(item => <li key={item.path}><button className={active?.path === item.path ? "selected" : ""} aria-current={active?.path === item.path ? "true" : undefined}
              onClick={() => {setSelection(item.path); setResource("");}}><span className="capability-entry-name">{mode === "skills" ? <FileText size={14}/> : <Bot size={14}/>}<strong>{item.name}</strong></span></button></li>)}
          </ul></details> : null;
        })}
      </aside>
      <article className="capability-detail">
        {active ? <>
          <header className="capability-detail-heading"><div><span className="capability-kind">{mode === "skills" ? "Skill" : "Subagent descriptor"}</span><h2>{active.name}</h2></div></header>
          <p className="instruction-description">{active.description}</p>
          <div className="capability-file-metadata"><code>{path}</code></div>
          {active.skills && <div className="instruction-related"><span>Relevant skills</span>{active.skills.map(name => <code key={name}>{name}</code>)}</div>}
          {(!!active.resources.length || path !== active.path) && <label className="instruction-resource">Resource<select aria-label="Instruction resource" value={path} onChange={event => setResource(event.target.value)}>
            <option value={active.path}>{mode === "skills" ? "SKILL.md" : "Descriptor"}</option>{active.resources.map(name => <option key={name} value={name}>{name.slice(active.path.lastIndexOf("/") + 1)}</option>)}
            {path !== active.path && !active.resources.includes(path || "") && <option value={path}>{path}</option>}
          </select></label>}
          <div className="capability-editor-toolbar"><div role="tablist" aria-label="Installed instruction view">
            <button role="tab" aria-selected={view === "preview"} onClick={() => setView("preview")}>Preview</button>
            <button role="tab" aria-selected={view === "source"} onClick={() => setView("source")}>Source</button>
          </div></div>
          <ErrorNotice error={file.error} stale={!!file.data}/>
          {file.data ? view === "preview" && path?.endsWith(".md") ? <div className="capability-preview" onClick={event => {
            const anchor = (event.target as Element).closest("a");
            const href = anchor?.getAttribute("href");
            if (!href || /^(?:[a-z][a-z\d+.-]*:|\/\/|#)/i.test(href)) return;
            const target = new URL(href, `https://catalog.invalid/${path}`).pathname.slice(1);
            event.preventDefault();
            if (!catalog.data?.files.includes(target)) return;
            const owner = entries.find(item => item.path === target || item.resources.includes(target));
            setSearch("");
            if (owner) setSelection(owner.path);
            setResource(target);
          }}><RichText>{content}</RichText></div> : <pre className="instruction-source">{file.data.content}</pre> : <div className="capability-empty">{file.error ? "Instruction unavailable" : "Loading instruction..."}</div>}
        </> : <div className="capability-empty">{catalog.isPending ? "Loading catalog..." : "No matching instructions"}</div>}
      </article>
    </div>
  </>;
}
