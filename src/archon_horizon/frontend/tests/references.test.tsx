import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { ReferenceMetadata } from "../src/pipeline/DesktopReferences";
import { decodeReferenceDraft, referenceBody, referenceDraft, referenceDraftError, type ReferenceRecord } from "../src/pipeline/referenceCatalog";
import { ApiError, requestText } from "../src/pipeline/api";
import { projectDashboardUrl } from "../src/utils/navigation";

const record: ReferenceRecord = {id: "reference-1", project_id: "project-1", revision: 4,
  cite_key: "doe-2026", title: "Convexity and geometry", kind: "article", authors: ["Jane Doe", "A. Example"],
  issued_year: 2026, venue: "Geometry Journal", identifiers: {doi: "10.1234/example"}, urls: ["https://example.org/article"],
  abstract: "A concise reference.", metadata_source: "Publisher metadata", status: "active"};

test("an edit keeps its source revision and cannot rename a citation identity", () => {
  const draft = {...referenceDraft(record), cite_key: "accidental-rename", title: "Corrected title"};
  const body = referenceBody(draft, "another-project", true);
  assert.equal(body.expected_revision, 4);
  assert.equal(body.changes?.title, "Corrected title");
  assert.equal(Object.hasOwn(body.changes!, "cite_key"), false);
  assert.equal(Object.hasOwn(body.changes!, "project_id"), false);
  assert.equal(record.title, "Convexity and geometry");
});

test("blank optional editor values clear saved metadata and leave completeness to the API", () => {
  const draft = {...referenceDraft(record), authors: "\n", issued_year: "", venue: " ", doi: "", urls: "\n", abstract: "", metadata_source: ""};
  const body = referenceBody(draft, record.project_id, true);
  assert.deepEqual(body.changes, {kind: "article", title: record.title, authors: [], issued_year: null,
    venue: null, identifiers: {}, urls: [], abstract: null, metadata_source: null, status: "active"});
  assert.equal(referenceDraftError(draft, true), "");
});

test("new records retain author boundaries and explicit withdrawn status", () => {
  const draft = {...referenceDraft(), cite_key: "doe-2026", title: " New reference ", authors: "Doe, Jane\n\nExample, A.\n",
    issued_year: "2026", doi: " https://doi.org/10.1234/example ", withdrawn: true};
  const body = referenceBody(draft, record.project_id);
  assert.equal(body.project_id, record.project_id);
  assert.equal(body.cite_key, "doe-2026");
  assert.deepEqual(body.authors, ["Doe, Jane", "Example, A."]);
  assert.deepEqual(body.identifiers, {doi: "https://doi.org/10.1234/example"});
  assert.equal(body.status, "withdrawn");
});

test("validation allows incomplete bibliography but rejects ambiguous years and invalid keys", () => {
  const draft = {...referenceDraft(), title: "Partial reference", cite_key: "partial-reference"};
  assert.equal(referenceDraftError(draft), "");
  for (const issued_year of ["2026.5", "1e3", "unknown", "9007199254740992"]) assert.match(referenceDraftError({...draft, issued_year}), /whole number/);
  assert.match(referenceDraftError({...draft, cite_key: "Doe.2026"}), /citation key/);
});

test("saved drafts survive refresh while malformed storage cannot alter the payload shape", () => {
  const fallback = referenceDraft(record);
  const saved = {...fallback, title: "Unsaved correction", expected_revision: 2, unexpected: "discard"};
  const restored = decodeReferenceDraft(JSON.stringify(saved), fallback);
  assert.equal(restored.title, "Unsaved correction");
  assert.equal(restored.expected_revision, 2, "A refresh must not silently rebase stale edits");
  assert.equal(Object.hasOwn(restored, "unexpected"), false);
  for (const raw of ["{", "null", '"wrong"', JSON.stringify({...fallback, authors: []}), JSON.stringify({...fallback, kind: "executable"}), JSON.stringify({...fallback, expected_revision: -1})])
    assert.deepEqual(decodeReferenceDraft(raw, fallback), fallback);
});

test("reference details escape metadata and never activate unsafe URL schemes", () => {
  const html = renderToStaticMarkup(<ReferenceMetadata record={{...record, title: "<script>bad()</script>",
    urls: ["javascript:alert(1)", "data:text/html,malicious", "https://example.org/article"], abstract: "<img src=x onerror=alert(1)>"}}/>);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /href="(?:javascript|data):/);
  assert.match(html, /href="https:\/\/example.org\/article"/);
  assert.match(html, /rel="noopener noreferrer"/);
  assert.match(html, /10\.1234\/example/);
});

test("reference selection survives share links and is cleared on unrelated project views", () => {
  const source = "https://horizon.example/pipeline?reference=reference-1&session=stale";
  assert.equal(projectDashboardUrl("project-1", "references", {}, source).searchParams.get("reference"), "reference-1");
  assert.equal(projectDashboardUrl("project-1", "nodes", {}, source).searchParams.has("reference"), false);
});

test("BibTeX uses the authenticated text transport and retains API error behavior", async () => {
  const originalWindow = globalThis.window;
  const originalFetch = globalThis.fetch;
  globalThis.window = {setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout} as unknown as Window & typeof globalThis;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, "/api/v3/references/reference-1/bibtex");
      assert.equal(init?.credentials, "same-origin");
      assert.equal(new Headers(init?.headers).get("Accept"), "application/x-bibtex");
      return new Response("@article{doe-2026, title={Geometry}}", {headers: {"Content-Type": "application/x-bibtex"}});
    };
    assert.equal(await requestText("/references/reference-1/bibtex"), "@article{doe-2026, title={Geometry}}");
    globalThis.fetch = async () => Response.json({error: {message: "Not permitted"}}, {status: 403});
    await assert.rejects(requestText("/references/reference-1/bibtex"), error => error instanceof ApiError && error.status === 403 && error.message === "Not permitted");
  } finally {globalThis.window = originalWindow; globalThis.fetch = originalFetch;}
});
