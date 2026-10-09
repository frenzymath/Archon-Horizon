/** Reference editor conversions. Identifier validation and deduplication belong to the API. */
export const referenceKinds = ["article", "book", "inproceedings", "thesis", "report", "webpage", "dataset", "other"] as const;
export const identifierKinds = ["doi", "arxiv", "isbn", "pmid"] as const;
export type ReferenceRecord = {
  id: string; project_id: string; revision: number; cite_key: string;
  kind: typeof referenceKinds[number]; title: string; authors: string[];
  issued_year: number | null; venue: string | null;
  identifiers: Partial<Record<typeof identifierKinds[number], string>>;
  urls: string[]; abstract: string | null; metadata_source: string | null;
  status: "active" | "incomplete" | "withdrawn"; created_at?: string; updated_at?: string;
};
export type ReferenceDraft = {
  cite_key: string; kind: ReferenceRecord["kind"]; title: string; authors: string;
  issued_year: string; venue: string; doi: string; arxiv: string; isbn: string; pmid: string;
  urls: string; abstract: string; metadata_source: string; withdrawn: boolean; expected_revision: number;
};

export function referenceDraft(record?: ReferenceRecord): ReferenceDraft {
  return {cite_key: record?.cite_key || "", kind: record?.kind || "article", title: record?.title || "",
    authors: (record?.authors || []).join("\n"), issued_year: record?.issued_year == null ? "" : String(record.issued_year),
    venue: record?.venue || "", doi: record?.identifiers.doi || "", arxiv: record?.identifiers.arxiv || "",
    isbn: record?.identifiers.isbn || "", pmid: record?.identifiers.pmid || "", urls: (record?.urls || []).join("\n"),
    abstract: record?.abstract || "", metadata_source: record?.metadata_source || "", withdrawn: record?.status === "withdrawn",
    expected_revision: record?.revision || 1};
}

/** Reject corrupted storage rather than silently turning it into an editor payload. */
export function decodeReferenceDraft(raw: string | null, fallback: ReferenceDraft): ReferenceDraft {
  if (!raw) return fallback;
  try {
    const value = JSON.parse(raw);
    if (!value || typeof value !== "object" || Object.entries(fallback).some(([key, item]) => typeof value[key] !== typeof item) ||
        !referenceKinds.includes(value.kind) || !Number.isSafeInteger(value.expected_revision) || value.expected_revision < 1) return fallback;
    return Object.fromEntries(Object.keys(fallback).map(key => [key, value[key]])) as ReferenceDraft;
  } catch {return fallback;}
}

const lines = (value: string) => value.split("\n").map(item => item.trim()).filter(Boolean);

export function referenceDraftError(draft: ReferenceDraft, editing = false): string {
  if (!draft.title.trim()) return "A reference title is required.";
  if (!editing && !/^[a-z][a-z0-9_-]{0,63}$/.test(draft.cite_key))
    return "Use a citation key beginning with a lowercase letter, followed by lowercase letters, digits, underscores or hyphens (up to 64 characters).";
  if (draft.issued_year.trim() && (!/^-?\d+$/.test(draft.issued_year.trim()) || !Number.isSafeInteger(Number(draft.issued_year))))
    return "The publication year must be a whole number.";
  return "";
}

/** Citation keys are stable API identities; edits deliberately omit them. */
export function referenceBody(draft: ReferenceDraft, projectId: string, editing = false) {
  const values = {kind: draft.kind, title: draft.title.trim(), authors: lines(draft.authors),
    issued_year: draft.issued_year.trim() ? Number(draft.issued_year) : null,
    venue: draft.venue.trim() || null,
    identifiers: Object.fromEntries(identifierKinds.filter(key => draft[key].trim()).map(key => [key, draft[key].trim()])),
    urls: lines(draft.urls), abstract: draft.abstract.trim() || null,
    metadata_source: draft.metadata_source.trim() || null, status: draft.withdrawn ? "withdrawn" : "active"};
  return editing ? {expected_revision: draft.expected_revision, changes: values} : {project_id: projectId, cite_key: draft.cite_key, ...values};
}
