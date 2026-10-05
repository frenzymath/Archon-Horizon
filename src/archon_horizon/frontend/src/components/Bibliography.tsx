import { useMemo } from "react";
import { ExternalLink } from "lucide-react";
import { bibliographyAnchor, parseBibliography, type BibliographyEntry } from "../utils/bibliography";
import "./blueprint.css";

export default function Bibliography({ bibtex, className = "" }: { bibtex: string; className?: string }) {
  const parsed = useMemo(() => {
    try { return { entries: parseBibliography(bibtex), error: "" }; }
    catch (error) { return { entries: [] as BibliographyEntry[], error: error instanceof Error ? error.message : "Could not parse BibTeX." }; }
  }, [bibtex]);
  return <div className={`platform-bibliography ${className}`}>
    {parsed.error && <div className="platform-bibliography-error" role="alert"><strong>Invalid BibTeX</strong><p>{parsed.error}</p></div>}
    {parsed.entries.length > 0 && <ol>{parsed.entries.map((entry, index) => <li id={bibliographyAnchor(entry.key)} key={`${entry.key}-${index}`}>
      <div className="platform-reference-heading"><span className="platform-reference-key">[{entry.key}]</span>{entry.authors && <span>{entry.authors}.</span>}{entry.year && <span>({entry.year}).</span>}</div>
      <div className="platform-reference-title">{entry.url ? <a href={entry.url} target="_blank" rel="noopener noreferrer">{entry.title}<ExternalLink size={12} /></a> : entry.title}</div>
      {(entry.venue || entry.detail) && <div className="platform-reference-venue"><em>{entry.venue}</em>{entry.detail && ` ${entry.detail}`}</div>}
      {entry.doi && <a className="platform-reference-doi" href={entry.url} target="_blank" rel="noopener noreferrer">doi:{entry.doi}</a>}
    </li>)}</ol>}
  </div>;
}
