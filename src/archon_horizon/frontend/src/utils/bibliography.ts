import { Cite } from "@citation-js/core";
import "@citation-js/plugin-bibtex";

export type BibliographyEntry = {
  key: string;
  authors: string;
  title: string;
  year: string;
  venue: string;
  detail: string;
  url?: string;
  doi?: string;
};

export function bibliographyAnchor(key: string): string {
  return `reference-${encodeURIComponent(key)}`;
}

export function safeReferenceUrl(value: unknown): string | undefined {
  if (typeof value !== "string" || !value.trim()) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : undefined;
  } catch { return undefined; }
}

export function parseBibliography(source: string): BibliographyEntry[] {
  if (!source.trim()) return [];
  // Force the local BibTeX parser; references must never trigger DOI/URL fetching.
  const entries = new Cite(source, { forceType: "@bibtex/text" }).data;
  if (!entries.length) throw new Error("No BibTeX entries found; expected @article{key, ...} or another BibTeX entry.");
  return entries.map((entry) => {
    const creators = Array.isArray(entry.author) ? entry.author : Array.isArray(entry.editor) ? entry.editor : [];
    const authors = creators.map((author: Record<string, any>) => author.literal || [author.given, author["dropping-particle"], author["non-dropping-particle"], author.family, author.suffix].filter(Boolean).join(" ")).join(", ");
    const year = entry.issued?.["date-parts"]?.[0]?.[0] ?? entry.issued?.literal ?? "";
    const doi = typeof entry.DOI === "string" ? entry.DOI.replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, "").trim() : undefined;
    return {
      key: String(entry["citation-key"] || entry.id || "reference"),
      authors,
      title: String(entry.title || "Untitled reference"),
      year: String(year),
      venue: String(entry["container-title"] || entry.publisher || ""),
      detail: [entry.volume, entry.issue ? `(${entry.issue})` : "", entry.page ? `pp. ${entry.page}` : ""].filter(Boolean).join(" "),
      url: doi ? `https://doi.org/${encodeURI(doi)}` : safeReferenceUrl(entry.URL),
      doi,
    };
  });
}

/** Parse every valid fenced BibTeX block without exposing malformed source in prose. */
export function bibliographyEntriesFromMarkdown(markdown: string): BibliographyEntry[] {
  const entries: BibliographyEntry[] = [];
  const fence = /^[ \t]*(`{3,}|~{3,})[ \t]*bibtex[ \t]*\r?\n([\s\S]*?)^[ \t]*\1[ \t]*$/gim;
  for (const match of markdown.matchAll(fence)) {
    try { entries.push(...parseBibliography(match[2])); }
    catch { /* Bibliography renders its own visible parse error for this block. */ }
  }
  return entries;
}

export function bibliographyEntry(entries: BibliographyEntry[], key: string): BibliographyEntry | undefined {
  return entries.find((entry) => entry.key === key) || entries.find((entry) => entry.key.toLowerCase() === key.toLowerCase());
}

export function citationLabel(entry: BibliographyEntry, locator?: string): string {
  const author = entry.authors
    ? (() => {
      const words = entry.authors.split(",")[0].trim().split(/\s+/);
      return words[words.length - 1] || entry.key;
    })()
    : entry.key;
  const suffix = [entry.year, locator?.trim()].filter(Boolean).join(", ");
  return suffix ? `${author}, ${suffix}` : author;
}

export function citationDetail(entry: BibliographyEntry, locator?: string): string {
  return [entry.authors, entry.year && `(${entry.year})`, entry.title, entry.venue, locator && `Location: ${locator}`]
    .filter(Boolean)
    .join(". ");
}
