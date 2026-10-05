# Reference catalog, cache, and blueprint evidence

## Reuse before adding

Search `GET /api/v3/references?project_id=...&q=...`, or use exact `doi` or
`cite_key` filters. Fetch the returned record before creating another entry.
The catalog checks identifier and citation-key uniqueness. Different versions
of an arXiv paper are not independent bibliographic works; retain the relevant
version and locator in the citation evidence.

Create new metadata with `POST /api/v3/records/reference`: `project_id`,
`cite_key`, `kind`, `title`, `authors`, and optional `issued_year`, `venue`,
`identifiers`, `urls`, `abstract`, `metadata_source`. Cite keys use lower-case
slugs beginning with a letter. Identifiers are structured fields (`doi`,
`arxiv`, `isbn`, `pmid`), not a freeform bibliography string. Missing authors or
year makes the reference incomplete; do not invent metadata to change its status.
For duplicates, inspect the returned existing entry instead of adding a suffix
to evade duplicate detection.

To correct existing metadata, use `PATCH /api/v3/records/reference/{id}` with
`expected_revision` and `changes`, using fields from `agent schema`. Reconcile
concurrent edits rather than replacing the revision on stale metadata.

## Cite and export

`POST /api/v3/reference-usages` registers the precise use and returns BibTeX:

```json
{
  "reference_id": "<reference-id>",
  "subject": {"kind":"node","id":"<node-id>"},
  "locator": "Theorem 3.2 and the standing completeness hypothesis in Section 3"
}
```

A file subject is `{"kind":"file","repository_id":"...","path":"Math/Result.lean"}`.
`GET /api/v3/references/{id}/bibtex` exports current metadata. To use the bounded
authenticated workspace cache:

```text
horizon-pipeline agent reference <reference-id> --workspace <absolute-prepared-workspace> --path
```

The returned file is a local cache, not the source of truth and not the paper
itself. Do not commit the cache tree. If the destination expects a `references.bib`,
merge verified exports with its existing bibliography using a BibTeX parser;
preserve custom fields and the project's selected citation convention. Update
existing keys when correcting metadata rather than duplicating citations.

## Blueprint and source notes

Inspect the destination's existing blueprint conventions before editing it.
Keep theorem/definition labels, declaration links, dependencies and citations
consistent with the reviewed mathematical statements. Do not generate a new
blueprint merely because a project has Lean files. A polished blueprint should
explain the result and proof dependencies, not reproduce a session transcript.

When a statement changes, update its blueprint passage and cited Lean locator
together, preserving the source scope and unresolved proof gaps. Check references,
cross-links and the repository's documented blueprint build when available.
Report unavailable tooling distinctly from a successful render. Keep large source
downloads and temporary render outputs out of the final library unless the
repository intentionally vendors those materials.
