# Project references

Select **References** in the main dashboard sidebar and choose a project. The
same catalog is available under **References** inside that project's dashboard.

The catalog stores
bibliographic metadata shared by the project's sessions. Search by title or
citation key, and use **Load more references** to browse another page. Selecting
an entry opens its authors, publication year, identifiers, source URLs and
abstract. The selected reference has a shareable dashboard URL.

**Show BibTeX** displays the API's current export. **Download BibTeX** retrieves
the same authenticated export as a `.bib` file. Metadata and links remain plain
text unless the link uses HTTP or HTTPS. Viewing metadata or generating BibTeX
does not retrieve the paper or resolve identifiers over the network.

## Add or correct metadata

A connected account with project write permission can select **Add reference**
or **Edit reference**. Authors and URLs use one entry per line. Citation keys
start with a lowercase letter and contain lowercase letters, digits, underscores
or hyphens, with at most 64 characters. A key remains fixed after creation so
existing citations retain their identity.

DOI, arXiv, ISBN and PMID fields are normalized and checked for duplicates by
the server. Search existing entries before adding a work; adding a suffix to
its key does not bypass identifier uniqueness. Missing authors or a publication
year makes an entry **incomplete**. Use **Withdrawn** for a withdrawn work;
preserve its identity instead of creating a replacement bibliography entry.

Drafts are retained in this browser tab's session storage, scoped by account,
project and entry. Closing the editor or refreshing the page retains its edits.
When another person or agent changes the record, the editor shows the latest
saved revision and retains your draft. Review the differences, then choose
**Keep my draft against revision …** or **Reload saved values**. Saving never
silently overwrites a newer revision.

If an acknowledgement is lost, **Retry saved request** sends the exact previous
request with its original idempotency key. Its fields stay locked until that
request is resolved. A read failure retains previously loaded data and offers a
retry. The API checks current permissions for every read and mutation.

## Source files

Reference details include **Files**. Upload PDFs, original TeX, source archives,
notes, or other companion material, with a description identifying the version
and an optional source URL. The files are stored on the control-plane host with
the project's durable artifacts and included in artifact backups. Registering
metadata alone does not download anything from publication URLs.

**Preview** opens a PDF or displays UTF-8 TeX/text without rendering HTML.
**Download** retrieves the original bytes with their filename. Archives and
other formats are downloadable; the server never extracts or executes them.
**Archive** hides an attachment while retaining its stored bytes. A different
version can use the same filename: both versions remain independently available.
Identical bytes with the same filename and provenance converge on one attachment.

The default limit is 64 MiB per file, configured with
`max_reference_file_bytes`, independently of the smaller JSON request limit.
Every list, preview, and download requires current project access. Writers can
upload or archive. A network failure keeps the chosen file and original request
key available for **Retry file upload** while the dialog remains open.

Dispatched agents use:

```sh
horizon-pipeline agent reference-files REFERENCE_UUID
horizon-pipeline agent reference-upload REFERENCE_UUID /absolute/paper-v1.pdf \
  --description "arXiv version 1" --source-url https://arxiv.org/pdf/2601.00001v1
horizon-pipeline agent reference-upload REFERENCE_UUID /absolute/paper-v1-source.tar.gz \
  --description "Original TeX for the same version"
horizon-pipeline agent reference-download REFERENCE_UUID FILE_UUID --output /absolute/scratch/paper.pdf
```

The upload client journals a private immutable copy before transfer, so replay
uses the original bytes even if the input file changes. Completed upload copies
are disposable; pending copies are retained for recovery. Downloads stream to
disk, verify SHA-256, and refuse to overwrite an existing output file. Inspect
PDFs and source files using the agent's normal reading/extraction tools; retain
the file ID, version, and precise page/theorem locator in citation evidence.
Use `--cursor` on `reference-files` for another page.

The same API is available to custom clients:

- `GET /api/v3/references/{id}/files`: paginated attachment metadata and upload limit.
- `POST /api/v3/references/{id}/files?filename=…&description=…&source_url=…`:
  raw file bytes, `Content-Type: application/octet-stream`, and `Idempotency-Key`.
- `GET /api/v3/references/{id}/files/{file_id}/content`: authenticated download;
  use `?preview=true` for supported previews. `X-Content-SHA256` identifies bytes.
- `POST /api/v3/references/{id}/files/{file_id}/archive`: empty JSON object and an
  idempotency key. Archiving preserves history and stored content.

## Agent access and citation evidence

Dispatched sessions use their provided credentials and the normal agent API
client. Search `GET /api/v3/references?project_id=…&q=…`; `doi` and `cite_key`
provide exact filters. The list uses opaque `cursor` values and a bounded
`limit`. Read an entry with `GET /api/v3/records/reference/{id}`.

Create metadata with `POST /api/v3/records/reference`. Correct it with
`PATCH /api/v3/records/reference/{id}`, supplying `expected_revision` and a
`changes` object. The dashboard uses these same revision and idempotency
contracts. The API returns identifier conflicts rather than silently merging
distinct works.

`POST /api/v3/reference-usages` records where a source supports the project:

```json
{
  "reference_id": "<reference-id>",
  "subject": {"kind": "node", "id": "<node-id>"},
  "locator": "Theorem 3.2 and the standing hypotheses in Section 3"
}
```

A file subject uses `{"kind":"file","repository_id":"…","path":"Math/Result.lean"}`.
Recording a use returns BibTeX; `GET /api/v3/references/{id}/bibtex` exports
metadata independently. The catalog editor does not itself create citation
evidence or modify a repository's bibliography.

## Workspace cache

Inside a dispatched session:

```sh
horizon-pipeline agent reference REFERENCE_UUID --workspace /absolute/workspace --path
```

This maintains a bounded workspace cache of authenticated BibTeX and returns its
local filename. Each retrieval revalidates authorization and the reference's
revision. The cache contains bibliographic exports, not papers, and does not
grant offline access or replace the central catalog. Keep the cache out of
version control. When a repository needs `references.bib`, merge verified
exports with a BibTeX parser while preserving custom fields and the project's
citation convention. See the [agent reference workflow](../src/archon_horizon/pipeline/skills/operations/source-research/references/catalog.md).
