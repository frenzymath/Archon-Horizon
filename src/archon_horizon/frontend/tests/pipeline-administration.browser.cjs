const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { build } = require("esbuild");
const { chromium } = require("playwright");

async function main() {
  const result = await build({
    stdin: {
      contents: `import React from 'react'; import {createRoot} from 'react-dom/client'; import {QueryClient,QueryClientProvider} from '@tanstack/react-query'; import {Settings,Resources,ProjectCreate} from './src/pipeline/Administration'; import './src/pipeline/pipeline.css';
      const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
      function App(){const [create,setCreate]=React.useState(false); const [resources,setResources]=React.useState(false); const [created,setCreated]=React.useState(''); const props={accountId:'admin',projectId:'project-1',writable:true,admin:true,search:'',run:undefined,command:async()=>true}; return <QueryClientProvider client={client}><main className="pl-app"><div className="pl-content"><button onClick={()=>setCreate(true)}>New project</button><button onClick={()=>setResources(!resources)}>Toggle resources</button><span role="status">{created}</span>{resources?<Resources {...props}/>:<Settings {...props}/>} {create&&<ProjectCreate accountId="admin" writable={true} onClose={()=>setCreate(false)} onCreated={p=>{setCreated(p.title);setCreate(false)}}/>}</div></main></QueryClientProvider>};createRoot(document.getElementById('root')).render(<App/>);`,
      resolveDir: path.join(__dirname, ".."),
      loader: "jsx",
      sourcefile: "administration-fixture.jsx",
    },
    bundle: true,
    write: false,
    outdir: "/virtual-assets",
    entryNames: "fixture",
    format: "esm",
    loader: { ".woff2": "dataurl", ".woff": "dataurl", ".ttf": "dataurl" },
    define: { "process.env.NODE_ENV": '"production"' },
  });
  const assets = new Map(
    result.outputFiles.map((file) => [
      `/${path.basename(file.path)}`,
      file.contents,
    ]),
  );
  let project = {
    id: "project-1",
    revision: 1,
    number: 1,
    title: "Poincare",
    slug: "poincare",
    description: "Initial description",
  };
  let host = {
    id: "host-1",
    revision: 1,
    slug: "worker-1",
    display_name: "Worker one",
    mode: "enabled",
    workspace_root: "/horizon/workspaces",
    scratch_root: "/horizon/scratch",
  };
  let harness = {
    id: "harness-1",
    revision: 1,
    slug: "codex",
    adapter: "codex_exec",
    provider_version: "0.153.4",
    enabled: true,
    model_options: { model: "gpt-6-astra", reasoning_effort: "medium" },
  };
  let capacity = {
    harness_id: harness.id,
    harness_slug: harness.slug,
    execution_slots: 2,
    max_parallel_subagents: 0,
    enabled: true,
    credential_configured: true,
  };
  let forceConflict = false,
    loseAcknowledgement = false,
    creations = 0;
  const receipts = new Map();
  const json = (response, value, status = 200) => {
    response.writeHead(status, { "Content-Type": "application/json" });
    response.end(JSON.stringify(value));
  };
  const page = (items) => ({ items, next_cursor: null });
  const server = http.createServer(async (request, response) => {
    const route = new URL(request.url, "http://localhost").pathname;
    if (route.startsWith("/api/v3")) {
      if (request.method === "PATCH" || request.method === "POST") {
        let raw = "";
        for await (const chunk of request) raw += chunk;
        const payload = JSON.parse(raw),
          key = request.headers["idempotency-key"];
        assert.ok(key);
        if (receipts.has(key)) return json(response, receipts.get(key));
        if (forceConflict) {
          forceConflict = false;
          project = {
            ...project,
            revision: project.revision + 1,
            description: "Updated elsewhere",
          };
          return json(
            response,
            { error: { message: "Revision conflict" } },
            409,
          );
        }
        let record;
        if (route === "/api/v3/records/project") {
          creations++;
          record = { id: "new-project", number: 2, ...payload };
        } else if (route.endsWith("/records/project/project-1")) {
          assert.equal(payload.expected_revision, project.revision);
          record = project = {
            ...project,
            ...payload.changes,
            revision: project.revision + 1,
          };
        } else if (route.endsWith("/hosts/host-1/harnesses/harness-1")) {
          assert.equal(payload.expected_revision, host.revision);
          host.revision++;
          capacity = { ...capacity, ...payload.changes };
          record = { ...capacity, host_revision: host.revision };
        } else if (route.endsWith("/records/host/host-1")) {
          assert.equal(payload.expected_revision, host.revision);
          record = host = {
            ...host,
            ...payload.changes,
            revision: host.revision + 1,
          };
        } else if (route.endsWith("/records/harness/harness-1")) {
          assert.equal(payload.expected_revision, harness.revision);
          record = harness = {
            ...harness,
            ...payload.changes,
            revision: harness.revision + 1,
          };
        } else return json(response, {}, 404);
        receipts.set(key, record);
        if (loseAcknowledgement) {
          loseAcknowledgement = false;
          return json(
            response,
            { error: { message: "Proxy lost the response" } },
            503,
          );
        }
        return json(response, record);
      }
      if (route.endsWith("/records/project/project-1"))
        return json(response, project);
      if (route.endsWith("/records/host")) return json(response, page([host]));
      if (route.endsWith("/records/harness"))
        return json(response, page([harness]));
      if (route.endsWith("/hosts/host-1/harnesses/harness-1"))
        return json(response, capacity);
      if (route.endsWith("/hosts/host-1/harnesses"))
        return json(response, {
          items: [capacity],
          host_revision: host.revision,
        });
      if (route.endsWith("/records/integration"))
        return json(
          response,
          page([
            {
              id: "forge",
              kind: "forge",
              endpoint: "http://127.0.0.1:3000",
              enabled: true,
              credential_ref: "secret:do-not-display",
            },
          ]),
        );
      if (route.endsWith("/projects/project-1/integrations"))
        return json(
          response,
          page([{ id: "forge", public_url: "https://forge.example" }]),
        );
      if (route.endsWith("/settings"))
        return json(response, {
          revision: 1,
          configuration: {
            public_url: "https://horizon.example",
            state_root: "/horizon",
            search_enabled: true,
          },
        });
      if (route.endsWith("/resources"))
        return json(response, {
          hosts: [
            {
              id: host.id,
              name: host.display_name,
              status: "enabled",
              occupied_slots: 1,
              slots: 2,
              heartbeat_at: "2026-09-29T10:00:00Z",
            },
          ],
          storage: [
            {
              category: "Evidence",
              bytes: 1e9,
              protected_bytes: 1e9,
              reclaimable_bytes: 0,
            },
          ],
          observed_at: "2026-09-29T10:00:00Z",
          free_bytes: 20e9,
          oldest_pending_delivery: null,
          backup_status: "Verified",
          provider_status: "Available",
        });
      return json(response, {}, 404);
    }
    if (assets.has(route)) {
      response.writeHead(200, {
        "Content-Type": route.endsWith(".css") ? "text/css" : "text/javascript",
      });
      response.end(assets.get(route));
      return;
    }
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end(
      '<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/fixture.css"><style>body{margin:0;font-family:Arial,sans-serif}button{cursor:pointer}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>',
    );
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
  });
  const artifacts = path.join(
    process.env.HOME,
    ".horizon/development-tmp/dashboard-administration",
  );
  fs.mkdirSync(artifacts, { recursive: true });
  try {
    const tab = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    tab.on("pageerror", (error) => {
      errors.push(error.message);
      console.error(error.message);
    });
    await tab.goto(`http://127.0.0.1:${server.address().port}`);
    await tab.getByLabel("Name", { exact: true }).fill("Poincare updated");
    await tab
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab.getByText("Saved", { exact: true }).waitFor();
    assert.equal(project.title, "Poincare updated");
    assert.ok(
      await tab.getByLabel("Description", { exact: true }).count(),
      await tab.locator("body").innerText(),
    );
    forceConflict = true;
    await tab.getByLabel("Description", { exact: true }).fill("Keep my draft");
    await tab
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab
      .getByText("This record changed elsewhere.", { exact: false })
      .waitFor();
    assert.equal(
      await tab.getByLabel("Description", { exact: true }).inputValue(),
      "Keep my draft",
    );
    await tab.getByRole("button", { name: "Reload saved values" }).click();
    await tab.getByRole("tab", { name: "Machines", exact: true }).click();
    await tab.getByLabel("Scheduling slot limit").fill("4");
    await tab
      .locator(".pl-admin-capacity")
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab
      .locator(".pl-admin-capacity")
      .getByText("Saved", { exact: true })
      .waitFor();
    assert.equal(capacity.execution_slots, 4);
    await tab.screenshot({
      path: path.join(artifacts, "machines-desktop.png"),
      fullPage: true,
    });
    await tab.getByRole("tab", { name: "Harnesses", exact: true }).click();
    await tab.getByLabel("Default model").fill("test-model");
    await tab
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab.getByText("Saved", { exact: true }).waitFor();
    assert.equal(harness.model_options.model, "test-model");
    await tab.getByRole("tab", { name: "Connections", exact: true }).click();
    await tab.getByText("Credential reference configured").waitFor();
    assert.ok(
      !(await tab.locator("body").innerText()).includes("do-not-display"),
    );
    assert.equal(
      await tab
        .getByRole("link", { name: "https://forge.example" })
        .getAttribute("href"),
      "https://forge.example",
    );
    assert.ok(!(await tab.locator("body").innerText()).includes("127.0.0.1"));
    await tab.getByRole("button", { name: "New project", exact: true }).click();
    await tab.getByLabel("Project name").fill("New geometry");
    assert.equal(
      await tab.getByLabel("Project identifier").inputValue(),
      "new-geometry",
    );
    loseAcknowledgement = true;
    await tab
      .getByRole("button", { name: "Create project", exact: true })
      .click();
    await tab
      .getByRole("button", { name: "Retry creation", exact: true })
      .waitFor();
    await tab.reload();
    await tab.getByRole("button", { name: "New project", exact: true }).click();
    await tab
      .getByRole("button", { name: "Retry creation", exact: true })
      .click();
    await tab.getByText("New geometry", { exact: true }).waitFor();
    assert.equal(creations, 1);
    await tab.setViewportSize({ width: 390, height: 844 });
    for (const name of ["Project", "Machines", "Harnesses", "Connections"]) {
      await tab.getByRole("tab", { name, exact: true }).click();
      assert.equal(
        await tab
          .locator("body")
          .evaluate((body) => body.scrollWidth <= innerWidth),
        true,
        `${name} overflows`,
      );
    }
    await tab.screenshot({
      path: path.join(artifacts, "connections-mobile.png"),
      fullPage: true,
    });
    await tab.getByRole("button", { name: "Toggle resources" }).click();
    await tab.getByRole("heading", { name: "Machines", exact: true }).waitFor();
    await tab.getByText("1 running / 2 configured").waitFor();
    assert.equal(
      await tab
        .locator("body")
        .evaluate((body) => body.scrollWidth <= innerWidth),
      true,
      "Resources overflows",
    );
    await tab.screenshot({
      path: path.join(artifacts, "resources-mobile.png"),
      fullPage: true,
    });
    assert.deepEqual(errors, []);
    console.log(
      "Administration browser checks passed: project editing/conflict, capacity, models, secret masking, project creation acknowledgement recovery, desktop/mobile layout.",
    );
  } finally {
    await browser.close();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
