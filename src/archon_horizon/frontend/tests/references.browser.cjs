/* Isolated UI contract test. No live service, account or filesystem cache is used. */
const assert = require("node:assert/strict");
const http = require("node:http");
const path = require("node:path");
const {build} = require("esbuild");
const {chromium} = require("playwright");

async function main() {
  const bundle = await build({stdin: {contents: `import React from 'react'; import {createRoot} from 'react-dom/client';
    import {QueryClient,QueryClientProvider} from '@tanstack/react-query'; import References from './src/pipeline/DesktopReferences';
    import './src/platform.css'; const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
    function App(){const [referenceId,select]=React.useState(new URLSearchParams(location.search).get('reference')||'');
      const [writable,setWritable]=React.useState(true);window.setReferenceWritable=setWritable;
      window.refreshReferences=()=>client.invalidateQueries({queryKey:['pipeline','test-account']});
      return <QueryClientProvider client={client}><main style={{padding:24}}><References accountId="test-account" projectId="project-1" referenceId={referenceId} writable={writable}
        onSelect={id=>{history.replaceState({},'',id?'/?reference='+id:'/');select(id)}}/></main></QueryClientProvider>}
    createRoot(document.getElementById('root')).render(<App/>);`, resolveDir: path.join(__dirname, ".."), loader: "jsx", sourcefile: "reference-fixture.jsx"},
    bundle: true, write: false, outdir: "/virtual-assets", entryNames: "fixture", format: "esm",
    loader: {".woff2": "dataurl", ".woff": "dataurl", ".ttf": "dataurl"}, define: {"process.env.NODE_ENV": '"production"'}});
  const assets = new Map(bundle.outputFiles.map(file => ["/" + path.basename(file.path), file.contents]));
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (assets.has(pathname)) {response.writeHead(200, {"Content-Type": pathname.endsWith(".css") ? "text/css" : "text/javascript"}); response.end(assets.get(pathname)); return;}
    response.writeHead(200, {"Content-Type": "text/html"});
    response.end('<!doctype html><html><head><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>');
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({headless: true, args: ["--no-sandbox"]});
  try {
    const tab = await browser.newPage({viewport: {width: 1300, height: 1000}});
    const errors = [], reads = [], mutations = [], receipts = new Map();
    tab.on("pageerror", error => errors.push(error.message));
    let record = {id: "ref-1", project_id: "project-1", cite_key: "doe-2026", revision: 1, kind: "article",
      title: "Geodesic convexity", authors: ["Jane Doe"], issued_year: 2026, venue: "Geometry Journal",
      identifiers: {doi: "10.1234/geodesic"}, urls: ["javascript:alert(1)", "https://example.org/article"],
      abstract: "An important reference.", metadata_source: "Publisher", status: "active"};
    const second = {...record, id: "ref-2", cite_key: "other-2025", title: "Comparison geometry", issued_year: 2025};
    let created = null, loseAcknowledgement = false, unavailableReads = false;
    let storedFiles = [], loseUploadAcknowledgement = true;
    const fileReceipts = new Map(), uploadRequests = [];

    await tab.route("**/api/v3/**", async route => {
      const req = route.request(), url = new URL(req.url()), p = url.pathname.slice("/api/v3".length);
      const respond = json => route.fulfill({json});
      if (/^\/references\/[^/]+\/files$/.test(p)) {
        if (req.method() === "GET") return respond({items: storedFiles, next_cursor: null, max_upload_bytes: 67108864});
        const key = req.headers()["idempotency-key"], bytes = req.postDataBuffer();
        uploadRequests.push({key, bytes: bytes.toString("utf8"), url: req.url()});
        assert.equal(req.headers()["content-type"], "application/octet-stream");
        if (fileReceipts.has(key)) return respond(fileReceipts.get(key));
        const item = {id: "file-1", filename: url.searchParams.get("filename"), description: url.searchParams.get("description"),
          source_url: url.searchParams.get("source_url"), size_bytes: bytes.length, sha256: "a".repeat(64), media_type: "text/plain",
          previewable: true, content_url: "/api/v3/references/ref-1/files/file-1/content", created_at: "2026-10-09T00:00:00Z"};
        storedFiles.push(item); fileReceipts.set(key, item);
        if (loseUploadAcknowledgement) {loseUploadAcknowledgement = false; return route.abort("failed");}
        return respond(item);
      }
      if (p === "/references/ref-1/files/file-1/content") return route.fulfill({contentType: "text/plain", body: "\\section{Source}\n<script>not executed</script>"});

      if (req.method() !== "GET") {
        const body = req.postDataJSON(), key = req.headers()["idempotency-key"];
        mutations.push({path: p, body, key});
        if (receipts.has(key)) return respond(receipts.get(key));
        if (req.method() === "PATCH") {
          if (body.expected_revision !== record.revision) return route.fulfill({status: 409, json: {error: {message: "Revision changed"}}});
          record = {...record, ...body.changes, revision: record.revision + 1}; receipts.set(key, record); return respond(record);
        }
        created = {...body, id: "ref-new", revision: 1}; receipts.set(key, created);
        if (loseAcknowledgement) {loseAcknowledgement = false; return route.abort("failed");}
        return respond(created);
      }
      reads.push(req.url());
      if (unavailableReads) return route.fulfill({status: 403, json: {error: {message: "Reference read unavailable"}}});
      if (p === "/references") {
        assert.equal(url.searchParams.get("project_id"), "project-1");
        if (url.searchParams.get("q")) return respond({items: second.title.toLowerCase().includes(url.searchParams.get("q").toLowerCase()) ? [second] : [], next_cursor: null});
        return respond({items: url.searchParams.get("cursor") ? [second] : [record, ...(created ? [created] : [])], next_cursor: url.searchParams.get("cursor") ? null : "next-reference"});
      }
      if (p === "/records/reference/ref-1") return respond(record);
      if (p === "/records/reference/ref-2") return respond(second);
      if (p === "/records/reference/ref-new") return respond(created);
      if (p.endsWith("/bibtex")) return route.fulfill({contentType: "application/x-bibtex", body: "@article{doe-2026, title={Geodesic convexity}}"});
      throw new Error(`Unexpected fixture request: ${req.method()} ${p}`);
    });
    await tab.goto(`http://127.0.0.1:${server.address().port}/`);
    await tab.getByRole("button", {name: /Geodesic convexity/}).waitFor();
    await tab.getByRole("button", {name: "Load more references"}).click();
    await tab.getByRole("button", {name: /Comparison geometry/}).waitFor();
    assert.ok(reads.some(url => url.includes("cursor=next-reference")), "Pagination uses the API cursor");
    await tab.getByRole("searchbox", {name: "Search references"}).fill("Comparison");
    await tab.waitForFunction(() => ![...document.querySelectorAll('.desktop-reference-row')].some(item => item.textContent.includes('Geodesic convexity')));
    await tab.getByRole("searchbox", {name: "Search references"}).fill("");
    await tab.getByRole("button", {name: /Geodesic convexity/}).click();
    await tab.getByRole("dialog", {name: "Reference details"}).waitFor();
    assert.equal(await tab.locator('a[href^="javascript:"]').count(), 0);
    assert.equal(await tab.getByRole("link", {name: "https://example.org/article"}).getAttribute("rel"), "noopener noreferrer");
    await tab.getByRole("button", {name: "Show BibTeX"}).click();
    await tab.getByLabel("BibTeX source").waitFor();
    assert.match(await tab.getByLabel("BibTeX source").textContent(), /@article/);
    assert.match(await tab.getByRole("link", {name: "Download BibTeX"}).getAttribute("href"), /ref-1\/bibtex$/);

    await tab.getByLabel("Source file", {exact: true}).setInputFiles({name: "paper.tex", mimeType: "text/plain", buffer: Buffer.from("\\section{Source}")});
    await tab.getByLabel("Description", {exact: true}).fill("Original source, version 1");
    await tab.getByLabel("Source URL", {exact: true}).fill("https://example.org/source-v1");
    await tab.getByRole("button", {name: "Upload file", exact: true}).click();
    await tab.getByRole("button", {name: "Retry file upload", exact: true}).waitFor();
    assert.equal(await tab.getByLabel("Source file", {exact: true}).isDisabled(), true);
    await tab.getByRole("button", {name: "Retry file upload", exact: true}).click();
    await tab.getByRole("link", {name: "Download paper.tex", exact: true}).waitFor();
    assert.deepEqual(uploadRequests[0], uploadRequests[1], "Uncertain binary transfers retain the bytes, metadata and key");
    assert.equal(storedFiles.length, 1);
    await tab.getByRole("button", {name: "Preview paper.tex", exact: true}).click();
    await tab.getByLabel("Source: paper.tex", {exact: true}).waitFor();
    assert.match(await tab.getByLabel("Source: paper.tex", {exact: true}).textContent(), /<script>not executed/);
    await tab.getByRole("button", {name: "Close preview", exact: true}).click();

    await tab.getByRole("button", {name: "Edit reference"}).click();
    await tab.getByLabel("Title", {exact: true}).fill("Draft correction");
    record = {...record, title: "Concurrent correction", revision: 2};
    await tab.evaluate(() => window.refreshReferences());
    await tab.getByText("The saved reference changed. Your draft is retained.").waitFor();
    assert.equal(await tab.getByLabel("Title", {exact: true}).inputValue(), "Draft correction");
    assert.equal(await tab.getByRole("button", {name: "Save reference", exact: true}).isDisabled(), true);
    await tab.reload();
    await tab.getByText("The saved reference changed. Your draft is retained.").waitFor();
    assert.equal(await tab.getByLabel("Title", {exact: true}).inputValue(), "Draft correction");
    await tab.getByRole("button", {name: "Keep my draft against revision 2"}).click();
    await tab.getByRole("button", {name: "Save reference", exact: true}).click();
    await tab.getByRole("heading", {name: "Draft correction", exact: true}).waitFor();
    assert.equal(record.revision, 3);
    assert.equal(mutations[0].body.expected_revision, 2);
    assert.equal(Object.hasOwn(mutations[0].body.changes, "cite_key"), false);
    await tab.getByRole("button", {name: "Close reference", exact: true}).click();

    await tab.getByRole("button", {name: "Add reference", exact: true}).click();
    await tab.getByLabel("Citation key", {exact: true}).fill("new-reference");
    await tab.getByLabel("Title", {exact: true}).fill("New bibliography entry");
    loseAcknowledgement = true;
    await tab.getByRole("dialog").getByRole("button", {name: "Add reference", exact: true}).click();
    await tab.getByRole("button", {name: "Retry saved request"}).waitFor();
    const firstRequest = mutations.at(-1);
    assert.equal(await tab.getByLabel("Title", {exact: true}).isDisabled(), true);
    await tab.reload();
    await tab.getByRole("button", {name: "Retry saved request"}).waitFor();
    assert.equal(await tab.getByLabel("Title", {exact: true}).inputValue(), "New bibliography entry");
    await tab.getByRole("button", {name: "Retry saved request"}).click();
    await tab.getByRole("heading", {name: "New bibliography entry", exact: true}).waitFor();
    assert.deepEqual(mutations.at(-1), firstRequest, "Unconfirmed saves replay their exact request and idempotency key");
    assert.equal(receipts.size, 2, "Retry did not create a second reference");
    await tab.getByRole("button", {name: "Close reference", exact: true}).click();
    await tab.evaluate(() => window.setReferenceWritable(false));
    await tab.getByRole("button", {name: "Add reference", exact: true}).waitFor({state: "hidden"});
    assert.equal(await tab.getByRole("button", {name: "Add reference", exact: true}).count(), 0);
    await tab.getByRole("button", {name: /Draft correction/}).click();
    await tab.getByRole("dialog").waitFor();
    assert.equal(await tab.getByRole("button", {name: "Edit reference"}).count(), 0);
    assert.equal(await tab.getByLabel("Source file", {exact: true}).count(), 0);
    await tab.getByRole("button", {name: "Close reference", exact: true}).click();
    unavailableReads = true;
    await tab.evaluate(() => window.refreshReferences());
    await tab.getByRole("alert").filter({hasText: "Reference read unavailable"}).waitFor();
    assert.equal(await tab.getByRole("button", {name: /Draft correction/}).count(), 1, "Read errors retain the loaded catalog");
    unavailableReads = false;
    await tab.getByRole("button", {name: "Retry references"}).click();
    await tab.getByRole("alert").waitFor({state: "hidden"});
    assert.deepEqual(errors, []);
    console.log("References browser: search, pagination, BibTeX, safe links, stale revisions, draft refresh, exact retries, source-file upload/download/preview and permissions passed");
  } finally {await browser.close(); await new Promise(resolve => server.close(resolve));}
}
main().catch(error => {console.error(error); process.exitCode = 1;});
