import { useEffect, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { BookOpen, Download, ExternalLink, Plus, RefreshCw, Search, Settings2, X } from "lucide-react";
import RecordDates from "../components/RecordDates";
import { safeReferenceUrl } from "../utils/bibliography";
import { requestText } from "./api";
import { usePagedRead, useRead } from "./queries";
import { useSave } from "./Administration";
import { decodeReferenceDraft, identifierKinds, referenceBody, referenceDraft, referenceDraftError,
  referenceKinds, type ReferenceDraft, type ReferenceRecord } from "./referenceCatalog";
import "./DesktopReferences.css";
import ReferenceFiles from "./ReferenceFiles";

type Editor = {id: string} | null;
type Props = {accountId: string; projectId: string; referenceId?: string; writable: boolean;
  onSelect: (id: string) => void};

function storedEditor(key: string): Editor {
  try {const saved = JSON.parse(sessionStorage.getItem(key) || "null"); return saved && typeof saved.id === "string" ? {id: saved.id} : null;}
  catch {return null;}
}

/** Native dialog provides keyboard focus containment and restores focus on close. */
function Dialog({title, children, onClose}: {title: string; children: ReactNode; onClose: () => void}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {dialog.current?.showModal(); return () => dialog.current?.close();}, []);
  return <dialog ref={dialog} className="platform-modal desktop-reference-dialog" aria-label={title} onCancel={event => {event.preventDefault(); onClose();}}>
    <div className="platform-modal-head"><h2>{title}</h2><button type="button" className="platform-icon-button" aria-label="Close reference" onClick={onClose}><X size={17}/></button></div>
    {children}
  </dialog>;
}

/** Catalog metadata is plain text; only validated HTTP(S) URLs become clickable. */
export function ReferenceMetadata({record}: {record: ReferenceRecord}) {
  const identifiers = identifierKinds.filter(kind => record.identifiers[kind]);
  return <div className="desktop-reference-metadata">
    <h2>{record.title}</h2>
    <p><code>{record.cite_key}</code> · {record.kind} · <span className="platform-status quiet">{record.status}</span></p>
    <dl><dt>Authors</dt><dd>{record.authors.join("; ") || "Not recorded"}</dd>
      <dt>Publication year</dt><dd>{record.issued_year ?? "Not recorded"}</dd>
      <dt>Venue</dt><dd>{record.venue || "Not recorded"}</dd>
      {identifiers.map(kind => <div className="desktop-reference-metadata-row" key={kind}><dt>{kind === "arxiv" ? "arXiv" : kind.toUpperCase()}</dt><dd>{record.identifiers[kind]}</dd></div>)}
      {record.metadata_source && <><dt>Metadata source</dt><dd>{record.metadata_source}</dd></>}
    </dl>
    {record.urls.length > 0 && <section aria-label="Reference links"><h3>Links</h3><ul>{record.urls.map((url, index) => {
      const href = safeReferenceUrl(url);
      return <li key={`${index}:${url}`}>{href ? <a href={href} target="_blank" rel="noopener noreferrer">{url}<ExternalLink size={13}/></a> : <span>{url} (unavailable link)</span>}</li>;
    })}</ul></section>}
    {record.abstract && <section aria-label="Abstract"><h3>Abstract</h3><p className="desktop-reference-abstract">{record.abstract}</p></section>}
    <RecordDates item={record}/>
  </div>;
}

function BibTeX({accountId, projectId, record}: {accountId: string; projectId: string; record: ReferenceRecord}) {
  const path = `/references/${encodeURIComponent(record.id)}/bibtex`;
  const [open, setOpen] = useState(false);
  const bibtex = useQuery({queryKey: ["pipeline", accountId, "references", projectId, path, record.revision],
    queryFn: ({signal}) => requestText(path, {signal}), enabled: open});
  return <section className="desktop-reference-bibtex" aria-label="BibTeX export">
    <div className="platform-toolbar"><button className="platform-small-button" type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide BibTeX" : "Show BibTeX"}</button>
      <a className="platform-small-button" href={`/api/v3${path}`} download={`${record.cite_key}.bib`}><Download size={14}/>Download BibTeX</a></div>
    {open && <>
      {bibtex.error && <div role="alert" className="platform-error">{bibtex.error.message} <button type="button" onClick={() => void bibtex.refetch()}>Retry BibTeX</button></div>}
      {bibtex.isPending && <p role="status">Loading BibTeX...</p>}
      {bibtex.data !== undefined && <pre tabIndex={0} aria-label="BibTeX source">{bibtex.data}</pre>}
    </>}
  </section>;
}

function ReferenceForm({accountId, projectId, record, writable, onSaved, onClose}: {
  accountId: string; projectId: string; record?: ReferenceRecord; writable: boolean;
  onSaved: (record: ReferenceRecord) => void; onClose: () => void;
}) {
  const identity = record?.id || "new";
  const draftKey = `horizon.pipeline.reference-draft.${accountId}.${projectId}.${identity}`;
  const [draft, setDraft] = useState<ReferenceDraft>(() => {
    try {return decodeReferenceDraft(sessionStorage.getItem(draftKey), referenceDraft(record));}
    catch {return referenceDraft(record);}
  });
  const [storageError, setStorageError] = useState("");
  const action = useSave(accountId, `reference.${projectId}.${identity}`);
  useEffect(() => {
    try {sessionStorage.setItem(draftKey, JSON.stringify(draft)); setStorageError("");}
    catch {setStorageError("Draft storage is unavailable. Keep this editor open to retain your edits.");}
  }, [draftKey, draft]);
  const changed = !!record && draft.expected_revision !== record.revision;
  const validationError = referenceDraftError(draft, !!record);
  const locked = !writable || action.busy || action.uncertain;
  const update = (values: Partial<ReferenceDraft>) => {setDraft(current => ({...current, ...values})); action.reset();};
  return <form className="platform-editor desktop-reference-editor" onSubmit={async event => {
    event.preventDefault();
    if (!writable || (!action.uncertain && (changed || validationError))) return;
    // useSave retains the exact request and key after an uncertain acknowledgement.
    const saved = await action.save<ReferenceRecord>(record ? `/records/reference/${encodeURIComponent(record.id)}` : "/records/reference",
      referenceBody(draft, projectId, !!record), record ? "PATCH" : "POST");
    if (saved) {
      try {sessionStorage.removeItem(draftKey);} catch { /* Confirmed source data remains authoritative. */ }
      onSaved(saved);
    }
  }}>
    <fieldset disabled={locked}>
      <label>Citation key<input required readOnly={!!record} pattern="[a-z][a-z0-9_-]{0,63}" maxLength={64} value={draft.cite_key} onChange={event => update({cite_key: event.target.value})}/></label>
      <p className="platform-muted">Citation keys are stable within a project. Use lowercase letters, digits, underscores or hyphens, beginning with a letter.</p>
      <label>Title<input required value={draft.title} onChange={event => update({title: event.target.value})}/></label>
      <label>Type<select value={draft.kind} onChange={event => update({kind: event.target.value as ReferenceRecord["kind"]})}>{referenceKinds.map(kind => <option key={kind}>{kind}</option>)}</select></label>
      <label>Authors (one per line)<textarea rows={3} value={draft.authors} onChange={event => update({authors: event.target.value})}/></label>
      <div className="desktop-reference-fields"><label>Publication year<input inputMode="numeric" value={draft.issued_year} onChange={event => update({issued_year: event.target.value})}/></label>
        <label>Venue<input value={draft.venue} onChange={event => update({venue: event.target.value})}/></label></div>
      <div className="desktop-reference-fields">{identifierKinds.map(kind => <label key={kind}>{kind === "arxiv" ? "arXiv" : kind.toUpperCase()}<input value={draft[kind]} onChange={event => update({[kind]: event.target.value})}/></label>)}</div>
      <label>URLs (one per line)<textarea rows={3} value={draft.urls} onChange={event => update({urls: event.target.value})}/></label>
      <label>Abstract<textarea rows={5} value={draft.abstract} onChange={event => update({abstract: event.target.value})}/></label>
      <label>Metadata source<input value={draft.metadata_source} onChange={event => update({metadata_source: event.target.value})}/></label>
      <label className="desktop-reference-checkbox"><input type="checkbox" checked={draft.withdrawn} onChange={event => update({withdrawn: event.target.checked})}/>Withdrawn</label>
      <p className="platform-muted">Entries without authors or a publication year remain incomplete. Identifiers are normalized and checked for duplicates when saved.</p>
    </fieldset>
    {changed && <div className="platform-stale-evidence" role="status"><p>The saved reference changed. Your draft is retained.</p>
      <details><summary>Current saved revision {record!.revision}</summary><ReferenceMetadata record={record!}/></details>
      <button type="button" className="platform-small-button" disabled={locked} onClick={() => update({expected_revision: record!.revision})}>Keep my draft against revision {record!.revision}</button>
      <button type="button" className="platform-text-button" disabled={locked} onClick={() => {setDraft(referenceDraft(record)); action.reset();}}>Reload saved values</button>
    </div>}
    {storageError && <p className="platform-error" role="alert">{storageError}</p>}
    {action.error && <p className="platform-error" role="alert">{action.error.message}</p>}
    {!writable && <p role="status">Editing is unavailable while disconnected or without write permission. Your draft is retained.</p>}
    {validationError && <p className="platform-muted" role="status">{validationError}</p>}
    <div className="platform-editor-actions"><button type="button" className="platform-small-button" disabled={action.busy} onClick={onClose}>Close editor</button>
      <button type="submit" className="platform-primary" disabled={!writable || action.busy || action.storageBlocked || (!action.uncertain && (changed || !!validationError))}>
        {action.busy ? "Saving..." : action.uncertain ? "Retry saved request" : record ? "Save reference" : "Add reference"}</button></div>
  </form>;
}

export default function DesktopReferences({accountId, projectId, referenceId = "", writable, onSelect}: Props) {
  const activeKey = `horizon.pipeline.reference-editor.${accountId}.${projectId}`;
  const [editor, setEditor] = useState<Editor>(() => storedEditor(activeKey));
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  useEffect(() => {const timer = window.setTimeout(() => setQuery(search), 180); return () => window.clearTimeout(timer);}, [search]);
  const listing = usePagedRead<ReferenceRecord>(accountId, "references", projectId,
    `/references?project_id=${encodeURIComponent(projectId)}&q=${encodeURIComponent(query)}&limit=50`);
  const activeId = editor ? editor.id : referenceId;
  const detail = useRead<ReferenceRecord>(accountId, "references", projectId,
    `/records/reference/${encodeURIComponent(activeId)}`, !!activeId);
  const records = listing.data?.pages.flatMap(page => page.items) || [];
  const activate = (value: Editor) => {
    setEditor(value);
    try {if (value) sessionStorage.setItem(activeKey, JSON.stringify(value)); else sessionStorage.removeItem(activeKey);} catch { /* The form independently retains the draft and request. */ }
  };
  const close = () => {activate(null); if (referenceId) onSelect("");};
  return <section className="desktop-references" aria-label="Project references">
    <div className="platform-document-heading"><h1>References</h1><div className="platform-document-actions"><button type="button" className="platform-icon-button" aria-label="Refresh references" disabled={listing.isFetching} onClick={() => void listing.refetch()}><RefreshCw size={16}/></button></div></div>
    <div className="platform-toolbar"><label className="platform-search-field"><Search size={16}/><input type="search" aria-label="Search references" placeholder="Search titles or citation keys" value={search} onChange={event => setSearch(event.target.value)}/></label>
      {writable && <button type="button" className="platform-primary" onClick={() => activate({id: ""})}><Plus size={15}/>Add reference</button>}</div>
    {listing.error && <div className="platform-error" role="alert">{listing.error.message} <button type="button" onClick={() => void listing.refetch()}>Retry references</button></div>}
    <p className="platform-muted" role="status">{listing.isPending ? "Loading references..." : `${records.length} references loaded${listing.hasNextPage ? " / more available" : ""}`}{listing.isFetching && !listing.isPending ? " / refreshing..." : ""}</p>
    <div className="desktop-reference-list">{records.map(record => <button type="button" className="platform-claim-row desktop-reference-row" key={record.id} onClick={() => onSelect(record.id)}>
      <BookOpen size={18}/><span className="platform-claim-copy"><strong>{record.title}</strong><small><code>{record.cite_key}</code> · {record.authors.join("; ") || "Authors not recorded"}{record.issued_year != null ? ` · ${record.issued_year}` : ""}</small></span><span className="platform-status quiet">{record.status}</span>
    </button>)}</div>
    {!listing.isPending && !listing.error && !records.length && <div className="platform-empty"><BookOpen size={28}/><strong>{query ? "No matching references" : "No references registered"}</strong></div>}
    {listing.hasNextPage && <div className="platform-node-pagination"><button type="button" className="platform-small-button" disabled={listing.isFetchingNextPage} onClick={() => void listing.fetchNextPage()}>{listing.isFetchingNextPage ? "Loading..." : "Load more references"}</button></div>}
    {(editor || referenceId) && <Dialog title={editor ? editor.id ? "Edit reference" : "Add reference" : "Reference details"} onClose={close}>
      {detail.error && activeId && <div className="platform-error" role="alert">{detail.error.message} <button type="button" onClick={() => void detail.refetch()}>Retry reference</button></div>}
      {detail.data && activeId && detail.data.project_id !== projectId ? <p role="alert" className="platform-error">This reference belongs to a different project.</p>
        : activeId && !detail.data ? <p role="status">{detail.error ? "Reference details are unavailable." : "Loading reference..."}</p>
        : editor ? <ReferenceForm key={editor.id || "new"} accountId={accountId} projectId={projectId} record={editor.id ? detail.data : undefined} writable={writable}
          onClose={close} onSaved={saved => {activate(null); onSelect(saved.id);}}/>
          : detail.data && <>
            {writable && <button type="button" className="platform-small-button" onClick={() => activate({id: detail.data!.id})}><Settings2 size={15}/>Edit reference</button>}
            <ReferenceMetadata record={detail.data}/><BibTeX key={detail.data.id} accountId={accountId} projectId={projectId} record={detail.data}/>
            <ReferenceFiles key={`files:${detail.data.id}`} accountId={accountId} projectId={projectId} referenceId={detail.data.id} writable={writable}/>
          </>}
    </Dialog>}
  </section>;
}
