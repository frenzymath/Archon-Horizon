const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { build } = require("esbuild");
const { chromium } = require("playwright");

async function main() {
  const bundle = await build({
    stdin: {
      contents: `import React from 'react'; import {createRoot} from 'react-dom/client'; import {QueryClient,QueryClientProvider} from '@tanstack/react-query'; import {DesktopHosts,DesktopAgents,DesktopAccounts} from './src/pipeline/DesktopAdministration'; import './src/platform.css';
    const client=new QueryClient({defaultOptions:{queries:{retry:false}}});function App(){const [tab,setTab]=React.useState('Execution hosts');const props={accountId:'admin',projectId:'',admin:true,writable:true};return <QueryClientProvider client={client}><main className="platform-shell"><header className="platform-topbar"><strong>Archon Horizon</strong><nav>{['Execution hosts','Agents','Accounts'].map(item=><button key={item} onClick={()=>setTab(item)}>{item}</button>)}</nav></header><div style={{padding:'28px 36px',background:'#fff',minHeight:'calc(100vh - 58px)'}}>{tab==='Execution hosts'?<DesktopHosts {...props}/>:tab==='Agents'?<DesktopAgents {...props}/>:<DesktopAccounts {...props}/>}</div></main></QueryClientProvider>}createRoot(document.getElementById('root')).render(<App/>);`,
      resolveDir: path.join(__dirname, ".."),
      loader: "jsx",
      sourcefile: "desktop-administration-fixture.jsx",
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
    bundle.outputFiles.map((file) => [
      "/" + path.basename(file.path),
      file.contents,
    ]),
  );
  const server = http.createServer((request, response) => {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (assets.has(pathname)) {
      response.writeHead(200, {
        "Content-Type": pathname.endsWith(".css")
          ? "text/css"
          : "text/javascript",
      });
      response.end(assets.get(pathname));
      return;
    }
    response.writeHead(200, { "Content-Type": "text/html" });
    response.end(
      '<!doctype html><html><head><link rel="stylesheet" href="/fixture.css"><style>body{margin:0;font-family:Arial,sans-serif}button{cursor:pointer}*{box-sizing:border-box}</style></head><body><div id="root"></div><script type="module" src="/fixture.js"></script></body></html>',
    );
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
  });
  try {
    const tab = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    tab.on("pageerror", (error) => errors.push(error.message));
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
    let reviewer = {
      id: "reviewer-1",
      revision: 1,
      project_id: "project-1",
      slug: "mathematical-fidelity",
      enabled: true,
      instructions:
        "Review the mathematical statement against its original source. Preserve every hypothesis.",
      invocation: "subrequest",
      functions: ["reviewer"],
      harness_id: null,
      model_options: {},
    };
    let reviewerConflict = false;
    const reviewerReads = [];
    const instructionReads = [];
    await tab.route("**/api/v3/**", async (route) => {
      const request = route.request(),
        url = new URL(request.url()),
        p = url.pathname.slice("/api/v3".length);
      const respond = (value) => route.fulfill({ json: value });
      const page = (items) => ({ items, next_cursor: null });
      if (p === "/instruction-catalog") return respond({revision:"fixture", files:["operations/horizon-pipeline/SKILL.md","lean/lean-check/SKILL.md","lean/lean-check/references/builds.md","subagents/reviewers/mathematical-fidelity.md"], skills:[
        {name:"horizon-pipeline",description:"Coordinate Horizon work.",category:"operations",path:"operations/horizon-pipeline/SKILL.md",resources:[]},
        {name:"lean-check",description:"Check changed Lean declarations.",category:"lean",path:"lean/lean-check/SKILL.md",resources:["lean/lean-check/references/builds.md"]},
      ],subagents:[
        {slug:"lean-worker",description:"Prove a bounded claim.",skills:["lean-check"],category:"implementation",source_path:"subagents/implementation/lean-worker.md"},
        {slug:"mathematical-fidelity",description:"Audit mathematical meaning.",skills:["lean-check"],category:"reviewers",source_path:"subagents/reviewers/mathematical-fidelity.md"}]});
      if (p === "/instruction-catalog/file") {
        const selected = url.searchParams.get("path");
        instructionReads.push(selected);
        return respond({path:selected,content:selected.endsWith("builds.md")?"# Build evidence\nTargeted checks. [Main workflow](../../../operations/horizon-pipeline/SKILL.md)":"---\nname: fixture\n---\n# Selected instruction\nUseful guidance.",instructions:selected.startsWith("subagents/reviewers/")?"# Review contract\nCheck exact PR head.":selected.startsWith("subagents/")?"# Specialist contract\nPreserve the intended target.":null,sha256:"fixture"});
      }
      if (request.method() === "PATCH") {
        const data = request.postDataJSON();
        assert.ok(request.headers()["idempotency-key"]);
        if (p === "/records/host/host-1") {
          assert.equal(data.expected_revision, host.revision);
          host = { ...host, ...data.changes, revision: host.revision + 1 };
          return respond(host);
        }
        if (p === "/records/harness/harness-1") {
          assert.equal(data.expected_revision, harness.revision);
          harness = {
            ...harness,
            ...data.changes,
            revision: harness.revision + 1,
          };
          return respond(harness);
        }
        if (p === "/records/reviewer_descriptor/reviewer-1") {
          if (reviewerConflict) {
            reviewerConflict = false;
            reviewer = {
              ...reviewer,
              revision: reviewer.revision + 1,
              instructions: "Another maintainer updated the instructions.",
            };
            return route.fulfill({
              status: 409,
              json: { error: { message: "Revision conflict" } },
            });
          }
          assert.equal(data.expected_revision, reviewer.revision);
          reviewer = {
            ...reviewer,
            ...data.changes,
            revision: reviewer.revision + 1,
          };
          return respond(reviewer);
        }
        return route.fulfill({ status: 404, json: {} });
      }
      if (p === "/resources")
        return respond({
          hosts: [
            {
              id: host.id,
              name: host.display_name,
              status: "storage_pressure",
              detail: "Storage blocked: 0.1 GiB free; 2.0 GiB required",
              slots: "4",
              occupied_slots: "2",
              heartbeat_at: "2026-09-29T15:00:00Z",
            },
          ],
          storage: [],
          observed_at: "2026-09-29T15:00:00Z",
          free_bytes: 20e9,
          oldest_pending_delivery: null,
          backup_status: "verified",
          provider_status: "available",
        });
      if (p === "/records/host") return respond(page([host]));
      if (p === "/hosts/host-1/harnesses")
        return respond({
          host_revision: host.revision,
          items: [
            {
              harness_id: harness.id,
              harness_slug: "codex",
              execution_slots: 4,
              enabled: true,
              credential_configured: true,
            },
          ],
        });
      if (p === "/records/harness") return respond(page([harness]));
      if (p === "/projects")
        return respond(
          page([
            { id: "project-1", number: 1, title: "Poincare", slug: "poincare" },
            { id: "project-2", number: 2, title: "Geometry", slug: "geometry" },
          ]),
        );
      if (p === "/records/reviewer_descriptor") {
        const project = url.searchParams.get("project_id");
        reviewerReads.push(project);
        return respond(
          page(
            project === "project-1"
              ? [reviewer]
              : [
                  {
                    ...reviewer,
                    id: "reviewer-2",
                    project_id: "project-2",
                    slug: "geometry-reviewer",
                    instructions: "Review the geometry statements.",
                  },
                ],
          ),
        );
      }
      if (p === "/auth/me")
        return respond({
          id: "admin",
          username: "axel",
          role: "admin",
          permissions: { admin: true, write: true },
        });
      return route.fulfill({
        status: 404,
        json: { error: { message: "Unknown fixture route" } },
      });
    });
    await tab.goto(`http://127.0.0.1:${server.address().port}`);
    await tab.getByRole("heading", { name: "Worker one" }).waitFor();
    await tab.getByText("Storage blocked: 0.1 GiB free; 2.0 GiB required").waitFor();
    assert.equal(
      await tab.locator(".platform-global-capacity b").innerText(),
      "4",
    );
    assert.match(
      await tab.locator(".platform-hosts-overview").innerText(),
      /2 active sessions/,
    );
    assert.equal(await tab.locator(".platform-slot.busy").count(), 2);
    const artifacts = path.join(
      process.env.HOME,
      ".horizon/development-tmp/desktop-administration",
    );
    fs.mkdirSync(artifacts, { recursive: true });
    await tab.screenshot({
      path: path.join(artifacts, "execution-hosts.png"),
      fullPage: true,
    });
    await tab.getByRole("button", { name: "Edit Worker one" }).click();
    await tab.getByLabel("Machine name").fill("Worker renamed");
    await tab
      .locator(".pl-admin-form")
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab.getByText("Saved", { exact: true }).waitFor();
    assert.equal(host.display_name, "Worker renamed");
    await tab.getByRole("button", { name: "Close dialog" }).click();
    await tab.getByRole("button", { name: "Agents", exact: true }).click();
    await tab.getByLabel("Default model").fill("test-model");
    await tab
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab.getByText("Saved", { exact: true }).waitFor();
    assert.equal(harness.model_options.model, "test-model");
    await tab.screenshot({
      path: path.join(artifacts, "agent-harnesses.png"),
      fullPage: true,
    });
    await tab.getByRole("tab", { name: "Reviewers" }).click();
    await tab.getByRole("heading", {name:"mathematical-fidelity", exact:true}).waitFor();
    assert.equal(
      await tab
        .getByRole("combobox", { name: "Reviewer project" })
        .inputValue(),
      "project-1",
    );
    await tab
      .getByText(
        "Review the mathematical statement against its original source.",
        { exact: false },
      )
      .waitFor();
    await tab
      .getByRole("combobox", { name: "Reviewer project" })
      .selectOption("project-2");
    await tab
      .getByRole("heading", { name: "geometry-reviewer", exact: true })
      .waitFor();
    await tab
      .getByText("Review the geometry statements.", { exact: true })
      .waitFor();
    await tab
      .getByRole("combobox", { name: "Reviewer project" })
      .selectOption("project-1");
    await tab
      .getByRole("heading", { name: "mathematical-fidelity", exact: true })
      .waitFor();
    await tab.getByRole("tab", { name: "Edit", exact: true }).click();
    await tab
      .getByRole("textbox", { name: "Reviewer instructions" })
      .fill("Check statement fidelity and unnecessary hypotheses.");
    await tab
      .getByRole("combobox", { name: "Reviewer harness" })
      .selectOption("harness-1");
    await tab
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab.getByText("Saved", { exact: true }).waitFor();
    assert.equal(
      reviewer.instructions,
      "Check statement fidelity and unnecessary hypotheses.",
    );
    assert.equal(reviewer.harness_id, "harness-1");
    reviewerConflict = true;
    await tab
      .getByRole("textbox", { name: "Reviewer instructions" })
      .fill("Preserve this draft after a conflict.");
    tab.once("dialog", dialog => void dialog.dismiss());
    await tab.getByRole("combobox", {name:"Reviewer project"}).selectOption("project-2");
    assert.equal(await tab.getByRole("combobox", {name:"Reviewer project"}).inputValue(),"project-1");
    await tab
      .getByRole("button", { name: "Save changes", exact: true })
      .click();
    await tab
      .getByText("This record changed elsewhere.", { exact: false })
      .waitFor();
    assert.equal(
      await tab
        .getByRole("textbox", { name: "Reviewer instructions" })
        .inputValue(),
      "Preserve this draft after a conflict.",
    );
    await tab
      .getByRole("button", { name: "Reload saved values", exact: true })
      .click();
    assert.equal(
      await tab
        .getByRole("textbox", { name: "Reviewer instructions" })
        .inputValue(),
      "Another maintainer updated the instructions.",
    );
    await tab.getByRole("tab", { name: "Preview", exact: true }).click();
    assert.ok(
      reviewerReads.includes("project-1") &&
        reviewerReads.includes("project-2"),
    );
    assert.ok(reviewerReads.every(Boolean));
    await tab.screenshot({
      path: path.join(artifacts, "agent-reviewers.png"),
      fullPage: true,
    });
    await tab.getByRole("tab", {name:"Skills",exact:true}).click();
    await tab.getByText("2 skills / Installed catalog", {exact:true}).waitFor();
    await tab.getByRole("heading", {name:"Selected instruction"}).waitFor();
    assert.equal(instructionReads.length,1);
    await tab.getByRole("button", {name:"lean-check",exact:true}).click();
    await tab.getByLabel("Instruction resource").selectOption("lean/lean-check/references/builds.md");
    await tab.getByRole("heading", {name:"Build evidence"}).waitFor();
    await tab.screenshot({path:path.join(artifacts,"skills.png"),fullPage:true});
    await tab.getByRole("link",{name:"Main workflow",exact:true}).click();
    await tab.getByRole("heading",{name:"horizon-pipeline",exact:true}).waitFor();
    await tab.getByRole("heading",{name:"Selected instruction",exact:true}).waitFor();
    await tab.getByRole("tab", {name:"Subagent library",exact:true}).click();
    await tab.getByText("2 subagent descriptors / Installed catalog",{exact:true}).waitFor();
    await tab.getByRole("button",{name:"lean-worker",exact:true}).click();
    await tab.getByRole("heading",{name:"Specialist contract",exact:true}).waitFor();
    await tab.getByLabel("Search installed instructions").fill("mathematical");
    await tab.getByRole("heading", {name:"Review contract"}).waitFor();
    await tab.getByText("Subagent descriptor", {exact:true}).waitFor();
    await tab.screenshot({path:path.join(artifacts,"reviewer-library.png"),fullPage:true});
    await tab.getByRole("tab", {name:"Source",exact:true}).click();
    assert.match(await tab.locator(".instruction-source").innerText(), /name: fixture/);
    await tab.getByRole("tab", {name:"Reviewers",exact:true}).click();
    await tab.getByText("Another maintainer updated the instructions.", {exact:true}).waitFor();
    await tab.getByRole("button", { name: "Accounts", exact: true }).click();
    await tab.getByText("axel", { exact: true }).waitFor();
    await tab.screenshot({
      path: path.join(artifacts, "accounts.png"),
      fullPage: true,
    });
    assert.equal(
      await tab
        .locator("body")
        .evaluate((body) => body.scrollWidth <= innerWidth),
      true,
    );
    assert.deepEqual(errors, []);
    console.log(
      "Desktop administration passed: original host rows/edit dialog, agent directory/harness edits/reviewer preview, current account, desktop screenshots.",
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
