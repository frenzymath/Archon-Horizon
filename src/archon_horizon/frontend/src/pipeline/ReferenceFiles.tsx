import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, ExternalLink, Upload } from "lucide-react";
import { ApiError, request, requestText, type Page } from "./api";
import { usePagedRead } from "./queries";
import { safeReferenceUrl } from "../utils/bibliography";

type ReferenceFile = {id: string; filename: string; description: string; source_url: string | null;
  size_bytes: number; sha256: string; media_type: string; previewable: boolean; content_url: string; created_at: string};
type FilesPage = Page<ReferenceFile> & {max_upload_bytes: number};

function FilePreview({file, accountId}: {file: ReferenceFile; accountId: string}) {
  const url = file.content_url + "?preview=true";
  const text = useQuery({queryKey: ["pipeline", accountId, "reference-file", file.id, file.sha256],
    queryFn: ({signal}) => requestText(url.slice("/api/v3".length), {signal}), enabled: file.media_type === "text/plain"});
  if (file.media_type === "application/pdf") return <iframe title={`PDF: ${file.filename}`} className="desktop-reference-pdf" src={url}/>;
  return <>{text.isPending && <p role="status">Loading source...</p>}
    {text.error && <p role="alert" className="platform-error">{text.error.message}</p>}
    {text.data !== undefined && <pre tabIndex={0} aria-label={`Source: ${file.filename}`}>{text.data}</pre>}</>;
}

/** Files share catalog permissions. A failed acknowledgement retains the exact
 * File object and request key so Retry cannot silently upload different bytes. */
export default function ReferenceFiles({accountId, projectId, referenceId, writable}: {accountId: string; projectId: string; referenceId: string; writable: boolean}) {
  const path = `/references/${encodeURIComponent(referenceId)}/files`;
  const files = usePagedRead<ReferenceFile>(accountId, "references", projectId, path);
  const [file, setFile] = useState<File | null>(null);
  const [description, setDescription] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [preview, setPreview] = useState<string>("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const pending = useRef<{file: File; path: string; key: string} | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const maximum = (files.data?.pages[0] as FilesPage | undefined)?.max_upload_bytes ?? 64 * 1024 ** 2;
  const locked = busy || !!pending.current;
  const items = files.data?.pages.flatMap(page => page.items) || [];
  return <section className="desktop-reference-files" aria-label="Reference files">
    <h3>Files</h3>
    {files.error && <p role="alert" className="platform-error">{files.error.message} <button type="button" onClick={() => void files.refetch()}>Retry files</button></p>}
    {files.isPending && <p role="status">Loading files...</p>}
    {!files.isPending && !items.length && <p className="platform-muted">No source files stored. Add the paper, original TeX, or companion material.</p>}
    {items.map(item => <article key={item.id} className="desktop-reference-file">
      <strong>{item.filename}</strong><span className="platform-muted">{(item.size_bytes / 1024).toFixed(1)} KiB · {new Date(item.created_at).toLocaleDateString()}</span>
      {item.description && <p>{item.description}</p>}
      {item.source_url && safeReferenceUrl(item.source_url) && <a href={safeReferenceUrl(item.source_url)!} target="_blank" rel="noopener noreferrer">Source<ExternalLink size={13}/></a>}
      <div className="platform-toolbar"><a className="platform-small-button" href={item.content_url} download={item.filename}><Download size={14}/>Download {item.filename}</a>
        {item.previewable && <button type="button" className="platform-small-button" onClick={() => setPreview(preview === item.id ? "" : item.id)}>{preview === item.id ? "Close preview" : `Preview ${item.filename}`}</button>}
        {writable && <button type="button" className="platform-text-button" disabled={busy} onClick={async () => {
          if (!window.confirm(`Archive ${item.filename}? Its stored bytes will be retained.`)) return;
          setBusy(true); setError("");
          try {await request(`${path}/${item.id}/archive`, {method: "POST", body: "{}", headers: {"Idempotency-Key": crypto.randomUUID()}}); await files.refetch();}
          catch (e) {setError(e instanceof Error ? e.message : "Archive failed");} finally {setBusy(false);}
        }}>Archive</button>}</div>
      {preview === item.id && <FilePreview accountId={accountId} file={item}/>}
    </article>)}
    {files.hasNextPage && <button type="button" className="platform-small-button" disabled={files.isFetchingNextPage} onClick={() => void files.fetchNextPage()}>Load more files</button>}
    {writable && <form className="platform-editor" onSubmit={async event => {
      event.preventDefault(); if (busy || !file) return;
      if (!pending.current) {
        if (!file.size || file.size > maximum) {setError(`Choose a nonempty file up to ${(maximum / 1024 ** 2).toFixed(0)} MiB.`); return;}
        const query = new URLSearchParams({filename: file.name, description}); if (sourceUrl) query.set("source_url", sourceUrl);
        pending.current = {file, path: `${path}?${query}`, key: crypto.randomUUID()};
      }
      setBusy(true); setError("");
      try {
        const upload = pending.current;
        await request(upload.path, {method: "POST", body: upload.file, headers: {"Content-Type": "application/octet-stream", "Idempotency-Key": upload.key}});
        pending.current = null; setFile(null); setDescription(""); setSourceUrl(""); if (input.current) input.current.value = "";
        await files.refetch();
      } catch (e) {
        // Conclusive client-side rejections let the user correct metadata. A
        // timeout/network/server failure keeps this file and key for Retry.
        if (e instanceof ApiError && e.status >= 400 && e.status < 500 && ![408, 429].includes(e.status)) pending.current = null;
        setError(e instanceof Error ? e.message : "Upload failed");
      } finally {setBusy(false);}
    }}>
      <fieldset disabled={locked}><label>Source file<input ref={input} type="file" onChange={event => setFile(event.target.files?.[0] || null)}/></label>
        <label>Description<input value={description} placeholder="Version, edition, or what this file contains" onChange={event => setDescription(event.target.value)}/></label>
        <label>Source URL<input type="url" value={sourceUrl} placeholder="Where this version was obtained" onChange={event => setSourceUrl(event.target.value)}/></label></fieldset>
      <button type="submit" className="platform-small-button" disabled={busy || !file}><Upload size={14}/>{busy ? "Uploading..." : pending.current ? "Retry file upload" : "Upload file"}</button>
      {pending.current && !busy && <p role="status">Keep this dialog open to retry the same file upload.</p>}
    </form>}
    {error && <p role="alert" className="platform-error">{error}</p>}
  </section>;
}
