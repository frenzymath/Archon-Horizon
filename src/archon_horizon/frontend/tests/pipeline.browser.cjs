const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const now = "2026-09-28T18:30:00Z";
let milestoneBaseline = null;
const account = {
  id: "account-1",
  username: "reviewer",
  role: "admin",
  permissions: { admin: true, write: true },
  max_offline_replay_seconds: 604800,
};
const project = {
  id: "project-1",
  revision: 1,
  number: 1,
  title: "Geometry formalization",
  slug: "geometry",
  description: "Formalize convexity along geodesics.",
  // This fixture exercises the retained strict milestone approval interface.
  workflow: "milestones",
};
const run = {
  id: "run-12",
  number: 12,
  title: "Prove geodesic convexity",
  project_id: project.id,
  mission_id: "mission-1",
  phase: { kind: "formalization" },
  status: "active",
  revision: 1,
};
const usage = { tokens_in: 12000, tokens_out: 3000, cost_usd: 0.25 };
const session = {
  id: "assignment-500",
  revision: 1,
  run_id: run.id,
  label: "500",
  session_number: 500,
  title: "Establish convexity along the geodesic",
  role: "worker",
  status: "running",
  created_at: now,
  started_at: now,
  usage,
  models: ["gpt-6-astra"],
  efforts: ["medium"],
  skills: [],
  subagents: [
    {id: "native-review", title: "mathematical-fidelity", description: "Verify the statement against the original mathematics.", status: "succeeded", descriptor_revision: 2},
    {id: "native-custom", title: "check_shared_definitions", description: "Compare the shared definitions and suggest a smaller reusable interface.", status: "interrupted"},
  ],
  host_id: "worker-1",
  profile_id: "worker",
  elapsed_seconds: 120,
  agent_seconds: 120,
  attempt_count: 2,
  context: {},
  native: false,
  can_resume: false,
};
const queued = {
  ...session,
  id: "assignment-501",
  label: "501",
  session_number: 501,
  title: "Integrate the convexity result",
  status: "queued",
  queue_position: 1,
  context: {retained_continuation: true, admission: {summary: "Session #500 is completed or failed or cancelled", state: "waiting", reason: "Retained workspace on worker-1: R12/A500 is using this workspace", checked_at: now}},
  parent_session_id: session.id,
  can_resume: false,
};
const secondQueued = {
  ...queued,
  id: "assignment-502",
  label: "502",
  session_number: 502,
  title: "Review the convexity statement",
  queue_position: 2,
  context: {admission: {summary: "At least 1 open pull requests with label awaiting-review", state: "ready", reason: "1 open Forge items; threshold 1", checked_at: now}},
};
const events = [
  {
    id: "event-1",
    kind: "agent_message",
    title: "The derivative lemma now elaborates.",
    created_at: now,
    links: [],
    data: { text: "The derivative lemma now elaborates." },
  },
];
const nodes = [
  {
    id: "n1",
    label: "convexity-theorem",
    kind: "claim",
    title: "Convexity theorem",
    status: "open",
    labels: ["milestone", "informal_stated"],
    children: ["n2"],
    markdown: "Convexity along the geodesic.",
    created_at: now,
    updated_at: now,
  },
  {
    id: "n2",
    label: "derivative-bound",
    kind: "lemma",
    title: "Derivative bound",
    status: "open",
    labels: ["formally_stated"],
    children: [],
    markdown: "The derivative is bounded.",
    created_at: now,
    updated_at: now,
  },
];
const host = {
  id: "h1",
  revision: 1,
  slug: "worker-1",
  display_name: "worker-1",
  mode: "enabled",
  workspace_root: "/horizon/workspaces",
  scratch_root: "/horizon/scratch",
};
const harness = {
  id: "harness-1",
  revision: 1,
  slug: "codex",
  adapter: "codex_exec",
  provider_version: "0.153.4",
  enabled: true,
  model_options: { model: "gpt-6-astra", reasoning_effort: "medium" },
};
const streams = new Set(),
  operations = new Map(),
  counts = new Map(),
  unknownRoutes = [];
let outage = false,
  dropCommandAck = false,
  loggedIn = true,
  commandCount = 0;
const page = (items) => ({ items, next_cursor: null });
const missions = [
  {id: "mission-1", number: 1, title: run.title, objective: "Prove the original convexity statement.", status: "open", revision: 1, parent_id: null, created_at: now, updated_at: now, latest_run: run},
  {id: "mission-2", number: 2, title: "Derivative bound", objective: "Establish the derivative bound.", acceptance_criteria: ["The derivative bound is checked."], delegation_note: "Establish the bound used by the parent theorem.", max_open_children: 8, status: "open", revision: 1, parent_id: "mission-1", created_at: now, updated_at: now},
];
let missionConflict = false;
const json = (res, value, status = 200) => {
  res.writeHead(status, {
    "Content-Type": "application/json",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(value));
};
const activityRun = () => ({
  ...run,
  status: run.status === "active" ? "running" : run.status,
  project_title: project.title,
  created_at: now,
  started_at: now,
  session_count: 3,
  total_session_count: 3,
  subagent_count: 2,
  main_session_count: 1,
  delegated_session_count: 2,
  native_subagent_count: 2,
  usage,
  elapsed_seconds: 120,
  agent_seconds: 120,
  models: ["gpt-6-astra"],
  context: {},
  slot_capacity: 4,
});
const emit = (sequence, resources = ["assignments"]) => {
  for (const stream of streams)
    stream.write(
      `id: ${sequence}\ndata: ${JSON.stringify({ sequence, resources })}\n\n`,
    );
};
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1");
  counts.set(url.pathname, (counts.get(url.pathname) || 0) + 1);
  if (url.pathname.startsWith("/api/v2"))
    return json(
      res,
      { error: { message: "Legacy API must not be called" } },
      500,
    );
  if (url.pathname.startsWith("/api/v3")) {
    if (outage)
      return json(
        res,
        { error: { message: "Fixture control plane unavailable" } },
        503,
      );
    const route = url.pathname.slice("/api/v3".length);
    if (route === "/auth/login") {
      loggedIn = true;
      return json(res, account);
    }
    if (!loggedIn)
      return json(res, { error: { message: "Sign in required" } }, 401);
    if (route === "/auth/me") return json(res, account);
    if (route === "/auth/logout") {
      loggedIn = false;
      return json(res, {});
    }
    if (route === "/events") {
      res.writeHead(200, {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        Connection: "keep-alive",
      });
      res.write(": connected\n\n");
      res.write('event: gap\ndata: {"sequence":1,"gap":true,"resources":[]}\n\n');
      streams.add(res);
      req.on("close", () => streams.delete(res));
      return;
    }
    if (route === "/projects") return json(res, page([project]));
    if (route === "/references") {
      assert.equal(url.searchParams.get("project_id"), project.id);
      return json(res, page([]));
    }
    if (route === "/projects/project-1") return json(res, project);
    if (route === "/runs/run-12") return json(res, run);
    if (route === "/projects/project-1/dashboard/overview")
      return json(res, { ...project, markdown: project.description });
    if (route === "/projects/project-1/dashboard/graph-targets")
      return json(res, {target_repository_id: "workspace-repo", targets: [
        {id: "workspace-repo", slug: "workspace", purpose: "workspace"},
        {id: "library-repo", slug: "library", purpose: "library"},
      ]});
    if (route === "/projects/project-1/dashboard/nodes") {
      const search = (url.searchParams.get("search") || "").toLowerCase(),
        type = url.searchParams.get("node_type"), label = url.searchParams.get("label"), milestone = url.searchParams.get("milestone");
      const filtered = nodes.filter(node => `${node.title} ${node.label}`.toLowerCase().includes(search)
        && (!type || node.kind === type) && (!label || node.labels.includes(label))
        && (!milestone || node.labels.includes("milestone") === (milestone === "true")));
      const offset = Number(url.searchParams.get("offset") || 0), limit = Number(url.searchParams.get("limit") || 50);
      return json(res, {nodes: filtered.slice(offset, offset + limit), total: filtered.length, types: [...new Set(nodes.map(node => node.kind))].sort()});
    }
    if (route === "/projects/project-1/dashboard/nodes/resolve")
      return json(res, { nodes });
    if (route.startsWith("/projects/project-1/dashboard/nodes/"))
      return json(res, {
        node:
          nodes.find((node) => node.id === route.split("/").pop()) || nodes[0],
        nodes,
      });
    if (route === "/projects/project-1/dashboard/graph")
      return json(res, { nodes });
    if (route === "/projects/project-1/dashboard/objectives")
      return json(res, {
        items: [
          {
            id: "objective-1",
            title: "Convexity milestone",
            revision: 1,
            created_at: now,
            updated_at: now,
          },
        ],
      });
    if (route === "/projects/project-1/dashboard/objectives/objective-1")
      return json(res, {
        id: "objective-1",
        title: "Convexity milestone",
        metadata: {milestones: {root: "milestones/Convexity"}},
        markdown: "Prove convexity while preserving all hypotheses.",
        created_at: now,
        updated_at: now,
      });
    if (route === "/documents/objective-1/milestones")
      return json(res, {enabled: true, document: {id: "objective-1", title: "Convexity milestone", revision: 1, source_commit_oid: "a".repeat(40)},
        current_baseline_id: milestoneBaseline?.id || null, baselines: milestoneBaseline ? [milestoneBaseline] : [], checks: [{id: "check-1", kind: "contract"}],
        gates: [{id: "route-gate", title: "Choose convexity route", kind: "route"}, {id: "contract-gate", title: "Review convexity contracts", kind: "contract"}],
        nodes: [{key: "n1", title: "Convexity theorem", children: ["n2"], belongs_to: [], statement_status: "accepted", proof_status: "conditional",
          source_url: "/fixture-forge", milestone: {id: "M01", retired: false, contract: {declarations: ["Geometry.Convexity.alongGeodesic"]}, definitions: ["milestones/Convexity/Definitions/Geodesic.lean"]}},
          {key: "n2", title: "Uniform derivative bounds for normalized parameterized geodesic families", children: [], belongs_to: [],
            statement_status: "proposed", proof_status: "open", milestone: {id: "M02", retired: false, definitions: []}}]});
    if (route === "/milestones/baselines" && req.method === "POST") {
      let body = "";
      for await (const chunk of req) body += chunk;
      const decision = JSON.parse(body);
      assert.equal(decision.document_id, "objective-1");
      assert.equal(decision.expected_revision, 1);
      assert.equal(decision.check_id, "check-1");
      assert.equal(decision.route_gate_id, "route-gate");
      assert.equal(decision.contract_gate_id, "contract-gate");
      milestoneBaseline = {id: "baseline-1", source_commit_oid: "a".repeat(40), created_at: now};
      return json(res, milestoneBaseline);
    }
    if (route === "/records/mission" && req.method === "GET")
      return json(res, {items: missions.map(m => ({...m, roadmap_document_id: "objective-1"})), next_cursor: null});
    if (route === "/projects/project-1/dashboard/missions") {
      const search = (url.searchParams.get("search") || "").toLowerCase();
      const matches = missions.filter(item => `${item.title} ${item.objective}`.toLowerCase().includes(search));
      const ids = new Set(matches.map(item => item.id));
      for (const item of matches) {let parent = item.parent_id; while (parent) {ids.add(parent); parent = missions.find(item => item.id === parent)?.parent_id;}}
      return json(res, {items: missions.filter(item => ids.has(item.id)).map(item => ({...item, child_count: missions.filter(child => child.parent_id === item.id).length})),
        match_ids: matches.map(item => item.id), total: matches.length, next_offset: null});
    }
    if (route.startsWith("/projects/project-1/dashboard/missions/")) {
      const item = missions.find(item => item.id === route.split("/").pop());
      return json(res, {...item, parent_title: missions.find(parent => parent.id === item.parent_id)?.title || null});
    }
    if ((route.startsWith("/missions/") && req.method === "PATCH") || (route === "/records/mission" && req.method === "POST")) {
      let raw = ""; for await (const chunk of req) raw += chunk;
      const body = JSON.parse(raw);
      assert.ok(req.headers["idempotency-key"]);
      if (req.method === "POST") {
        const item = {...body, id: `mission-${missions.length + 1}`, number: missions.length + 1, revision: 1, status: "open", created_at: now, updated_at: now};
        missions.push(item); return json(res, item);
      }
      const item = missions.find(item => item.id === route.split("/").pop());
      if (missionConflict) {missionConflict = false; item.revision++; item.objective = "Concurrent saved objective."; return json(res, {error: {code: "revision_conflict", message: "Mission changed; refresh its revision."}}, 409);}
      assert.equal(body.expected_revision, item.revision);
      Object.assign(item, body, {revision: item.revision + 1}); return json(res, item);
    }
    if (route === "/projects/project-1/integrations") {
      const base = `http://${req.headers.host}`;
      return json(
        res,
        page([
          {
            id: "forge",
            kind: "forge",
            public_url: `${base}/fixture-forge`,
            enabled: true,
          },
          {
            id: "zulip",
            kind: "zulip",
            public_url: `${base}/fixture-zulip`,
            enabled: true,
          },
        ]),
      );
    }
    if (route === "/records/host") return json(res, page([host]));
    if (route === "/records/harness") return json(res, page([harness]));
    if (route === "/records/reviewer_descriptor") return json(res, page([]));
    if (route === "/records/project/project-1") return json(res, project);
    if (route === "/dashboard/activity/runs")
      return json(res, { runs: [{...activityRun(), id: "run-13", number: 13, phase: {kind: "postprocessing"}, status: "cancelled", title: "Improve the library"}, activityRun()], next_before: null });
    if (route === "/dashboard/activity/runs/run-13")
      return json(res, {...activityRun(), id: "run-13", number: 13, phase: {kind: "postprocessing"}, status: "cancelled", title: "Improve the library", sessions: []});
    if (route === "/dashboard/activity/runs/run-12") {
      if (url.searchParams.get("view") === "metrics") return json(res, activityRun());
      if (url.searchParams.get("view") === "queue") return json(res, {admissions: Object.fromEntries(
        [queued, secondQueued].map(value => [value.id, value.context.admission]))});
      return json(res, {
        ...activityRun(),
        metrics_pending: url.searchParams.get("compact") === "true",
        ...(url.searchParams.get("compact") === "true" ? {
          usage: {tokens_in: null, tokens_out: null, cost_usd: null}, agent_seconds: null, models: [], native_subagent_count: null,
        } : {}),
        sessions: [session, queued, secondQueued].map(value => {
          if (url.searchParams.get("compact") !== "true") return value;
          const {subagents, models, efforts, skills, usage, ...summary} = value;
          return summary;
        }),
      });
    }
    if (route.endsWith("/logs"))
      return json(res, { enabled: true, records: [] });
    if (route.endsWith("/events"))
      return json(res, { events, next_before: null });
    if (route.startsWith("/dashboard/activity/sessions/")) {
      const id = route.split("/").pop(),
        value = [session, queued, secondQueued].find((item) => item.id === id);
      const detail = {
        ...value,
        attempts: [
          {
            id: "execution-1",
            number: 1,
            status: "failed",
            host_id: "worker-1",
            started_at: now,
            finished_at: now,
          },
          {
            id: "execution-2",
            number: 2,
            status: "running",
            host_id: "worker-1",
            started_at: now,
          },
        ],
        reports: [
          {
            id: "report-1",
            revision: 1,
            kind: "context",
            markdown: "The derivative bound is established.",
            ledger: {mission: "The derivative bound is established.", acceptance_criteria: ["Deliver **source-bound** evidence."], items: [
              {id: "open-ledger", number: 1, description: "Verify the **endpoint**.", status: "open", comments: [{markdown: "Check the **boundary case**.", created_at: now}]},
              {id: "delegated-ledger", number: 2, description: "Prove the helper.", status: "handled", resolution: {assignment_ids: [queued.id]}},
            ]},
            created_at: now,
          },
        ],
        events,
        next_before: null,
      };
      if (url.searchParams.get("view") === "reports") return json(res, {reports: detail.reports});
      if (url.searchParams.get("compact") === "true") {
        delete detail.events; delete detail.reports; delete detail.next_before;
      }
      return json(res, detail);
    }
    if (route === "/resources")
      return json(res, {
        hosts: [
          {
            id: "h1",
            name: "worker-1",
            status: "enabled",
            slots: 4,
            occupied_slots: 2,
            heartbeat_at: now,
          },
        ],
        storage: [],
        observed_at: now,
        free_bytes: 20e9,
        oldest_pending_delivery: null,
        backup_status: "verified",
        provider_status: "available",
      });
    if (route === "/commands") {
      let body = "";
      for await (const chunk of req) body += chunk;
      const command = JSON.parse(body),
        id = req.headers["idempotency-key"];
      assert.ok(Number.isInteger(command.expected_revision));
      assert.ok(id);
      if (operations.has(id)) return json(res, operations.get(id));
      commandCount++;
      if (command.operation === "retry_assignment") {
        assert.equal(command.target_id, session.id);
        assert.equal(command.expected_revision, session.revision);
        session.status = "queued";
        session.can_resume = false;
        session.revision++;
      }
      if (
        command.operation === "move_before" ||
        command.operation === "move_after"
      ) {
        assert.equal(command.target_id, secondQueued.id);
        assert.equal(command.args.other_id, queued.id);
        secondQueued.revision++;
        secondQueued.queue_position = 1;
        queued.queue_position = 2;
      }
      if (command.operation === "cancel_run") {
        run.status = "cancelled";
        run.revision++;
      }
      const operation = { id, status: "completed", result: command };
      operations.set(id, operation);
      if (dropCommandAck) {
        dropCommandAck = false;
        return json(
          res,
          { error: { message: "Proxy lost the acknowledgement" } },
          503,
        );
      }
      return json(res, operation);
    }
    if (route.startsWith("/operations/"))
      return json(
        res,
        operations.get(route.split("/").pop()) || {
          error: { message: "Not found" },
        },
        operations.has(route.split("/").pop()) ? 200 : 404,
      );
    unknownRoutes.push(route);
    return json(
      res,
      { error: { message: `Unknown fixture route: ${route}` } },
      404,
    );
  }
  if (url.pathname.startsWith("/fixture-")) {
    res.writeHead(200, { "Content-Type": "text/html" });
    res.end(
      `<html><body><h1>${url.pathname === "/fixture-forge" ? "Forge repository" : "Zulip discussion"}</h1></body></html>`,
    );
    return;
  }
  const relative = url.pathname.startsWith("/assets/")
      ? url.pathname
      : "/index.html",
    file = path.join(__dirname, "..", "dist", relative);
  if (!fs.existsSync(file)) {
    res.writeHead(404);
    res.end();
    return;
  }
  res.writeHead(200, {
    "Content-Type": relative.endsWith(".js")
      ? "application/javascript"
      : relative.endsWith(".css")
        ? "text/css"
        : relative.endsWith(".html")
          ? "text/html"
          : "application/octet-stream",
  });
  fs.createReadStream(file).pipe(res);
});

(async () => {
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  if (process.env.HORIZON_BROWSER_PREVIEW === "1") {
    console.log(`Sample-data dashboard: ${base}/pipeline`);
    return;
  }
  const artifacts =
    process.env.HORIZON_BROWSER_ARTIFACTS ||
    path.join(
      process.env.HOME,
      ".horizon/development-tmp/pipeline-desktop/browser",
    );
  fs.mkdirSync(artifacts, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    args: ["--no-sandbox"],
  });
  try {
    const tab = await browser.newPage({
        viewport: { width: 1440, height: 1000 },
      }),
      errors = [];
    tab.on("pageerror", (error) => errors.push(error.message));
    await tab.goto(`${base}/pipeline`);
    await tab
      .getByRole("button", { name: new RegExp(project.title) })
      .waitFor();
    await tab.getByText("Connected", { exact: true }).waitFor();
    const startup = await tab.evaluate(() =>
      performance
        .getEntriesByType("resource")
        .map((item) => ({
          name: new URL(item.name).pathname,
          bytes: item.decodedBodySize,
        })),
    );
    assert.ok(
      !startup.some((item) =>
        /vizWorker|ActivityTab|DesktopActivity|DesktopAdministration|PlatformApp|PlatformEntry/.test(
          item.name,
        ),
      ),
      "Project directory eagerly loaded unrelated workspaces",
    );
    await tab.screenshot({
      path: path.join(artifacts, "projects-desktop.png"),
      fullPage: true,
    });
    await tab.getByRole("button", { name: "References", exact: true }).click();
    await tab.getByLabel("References project", { exact: true }).waitFor();
    await tab.getByText("No references registered", { exact: true }).waitFor();
    assert.equal(await tab.getByLabel("References project").inputValue(), project.id);
    await tab.getByRole("button", { name: "Forge", exact: true }).click();
    await tab.getByLabel("Forge project", { exact: true }).waitFor();
    await tab.frameLocator('iframe[title="Forge"]').getByRole("heading", { name: "Forge repository" }).waitFor();
    await tab.getByRole("button", { name: "Projects", exact: true }).click();
    await tab.getByRole("button", { name: new RegExp(project.title) }).click();
    await tab
      .getByRole("heading", { name: project.title, exact: true })
      .waitFor();
    const recentRuns = tab.getByLabel("Recent runs", {exact: true});
    await recentRuns.getByLabel("Phase: Post-processing", {exact: true}).waitFor();
    assert.match(await recentRuns.locator(".desktop-project-run").first().innerText(), /Cancelled/);
    await recentRuns.getByLabel("Phase: Main formalization", {exact: true}).waitFor();
    await tab.screenshot({path: path.join(artifacts, "project-run-phases-desktop.png"), fullPage: true});
    await tab.getByRole("button", { name: "Objectives", exact: true }).click();
    await tab.getByRole("button", { name: /Convexity milestone/ }).click();
    await tab
      .getByText("Prove convexity while preserving all hypotheses.", {
        exact: true,
      })
      .waitFor();
    const milestones = tab.getByRole("region", {name: "Milestones", exact: true});
    await milestones.getByRole("cell", {name: "conditional", exact: true}).waitFor();
    assert.equal(await milestones.getByRole("columnheader").allTextContents().then(values => values.join(",")), "Milestone,Statement,Proof");
    await milestones.getByText("Contract and dependencies", {exact: true}).first().click();
    await milestones.getByText("Geometry.Convexity.alongGeodesic", {exact: true}).waitFor();
    await milestones.getByText("Baseline approval", {exact: true}).click();
    await milestones.getByRole("combobox", {name: "Verification", exact: true}).selectOption("check-1");
    await milestones.getByRole("combobox", {name: "Route review", exact: true}).selectOption("route-gate");
    await milestones.getByRole("combobox", {name: "Contract review", exact: true}).selectOption("contract-gate");
    await milestones.getByLabel("Decision", {exact: true}).fill("Approve the inspected contracts.");
    await tab.screenshot({path: path.join(artifacts, "milestones-desktop.png"), fullPage: true});
    await tab.setViewportSize({width: 390, height: 844});
    assert.ok(await milestones.evaluate(element => element.scrollWidth <= element.clientWidth + 1), "Milestones overflow on mobile");
    await tab.screenshot({path: path.join(artifacts, "milestones-mobile.png"), fullPage: true});
    await tab.setViewportSize({width: 1440, height: 1000});
    await milestones.getByRole("button", {name: "Approve baseline", exact: true}).click();
    await milestones.getByText(/Baseline approved/).waitFor();
    await milestones.getByText("Launch run", {exact: true}).first().click();
    await milestones.getByLabel("Phase", {exact: true}).selectOption("formalization");
    await milestones.getByLabel("Approved baseline", {exact: true}).selectOption("baseline-1");
    await milestones.getByLabel("Mission", {exact: true}).selectOption("mission-1");
    await milestones.locator('.milestone-host input').first().check();
    assert.ok(await milestones.getByRole("button", {name: "Launch run", exact: true}).isEnabled());
    await tab.getByRole("button", { name: "Nodes", exact: true }).click();
    await tab.getByLabel("Milestone filter").selectOption("true");
    await tab.getByText("1 nodes", {exact: true}).waitFor();
    await tab.getByRole("button", {name: /Convexity theorem/}).waitFor();
    assert.equal(await tab.getByRole("button", {name: /Derivative bound/}).count(), 0);
    await tab.getByLabel("Node type", {exact: true}).selectOption("lemma");
    await tab.getByText("No matching nodes", {exact: true}).waitFor();
    await tab.getByLabel("Milestone filter").selectOption("false");
    await tab.getByRole("button", {name: /Derivative bound/}).waitFor();
    await tab.getByLabel("Node label", {exact: true}).selectOption("formally_stated");
    await tab.getByText("1 nodes", {exact: true}).waitFor();
    await tab.getByLabel("Search nodes", {exact: true}).fill("unmatched");
    await tab.getByText("No matching nodes", {exact: true}).waitFor();
    await tab.getByRole("button", {name: "Clear filters", exact: true}).click();
    await tab.getByText("2 nodes", {exact: true}).waitFor();
    await tab.getByLabel("Graph target repository").selectOption("library-repo");
    const targetRequest = tab.waitForRequest(request => request.url().includes("/dashboard/nodes") && request.url().includes("target_repository_id=workspace-repo"));
    await tab.getByLabel("Graph target repository").selectOption("workspace-repo");
    await targetRequest;
    await tab.getByRole("button", { name: /Convexity theorem/ }).click();
    await tab
      .getByRole("heading", { name: "Convexity theorem", exact: true })
      .waitFor();
    await tab.getByRole("button", { name: "DAG", exact: true }).click();
    await tab.locator(".formalization-dag-inner svg").waitFor();
    assert.ok(
      (await tab.locator("svg g.node").count()) >= 2,
      "Node DAG is blank",
    );
    const milestoneNode = tab.locator('svg g.node[data-node-id="n1"]');
    assert.equal(await milestoneNode.locator("ellipse").count(), 0);
    assert.equal(await milestoneNode.locator("polygon").count(), 1);
    await milestoneNode.press("Enter");
    assert.equal(await milestoneNode.getAttribute("aria-pressed"), "true");
    assert.equal(await tab.locator(".formalization-dag-milestone-symbol").count(), 1);
    await tab.screenshot({
      path: path.join(artifacts, "node-dag-desktop.png"),
      fullPage: true,
    });
    await tab
      .getByRole("button", { name: "Back to nodes", exact: true })
      .click();
    await tab.getByRole("button", { name: "Missions", exact: true }).click();
    await tab.getByText(run.title, { exact: true }).waitFor();
    await tab.getByRole("button", {name: "Preview Derivative bound", exact: true}).waitFor();
    assert.equal(await tab.locator('[data-mission-id="mission-2"]').evaluate(element => element.style.getPropertyValue("--mission-depth")), "1");
    await tab.getByRole("button", {name: `Collapse ${run.title}`, exact: true}).click();
    assert.equal(await tab.getByRole("button", {name: "Preview Derivative bound", exact: true}).count(), 0);
    await tab.getByRole("button", {name: "Expand all missions", exact: true}).click();
    await tab.getByRole("textbox", {name: "Search missions", exact: true}).fill("Derivative bound");
    await tab.getByText("1 of 1 missions", {exact: true}).waitFor();
    assert.equal(await tab.locator(".platform-mission-row").count(), 2, "Search lost the parent mission");
    await tab.getByRole("button", {name: "Edit Derivative bound", exact: true}).click();
    let missionDialog = tab.getByRole("dialog", {name: "Edit mission", exact: true});
    await missionDialog.getByLabel("Title", {exact: true}).fill("Derivative estimate");
    await tab.reload();
    missionDialog = tab.getByRole("dialog", {name: "Edit mission", exact: true});
    await missionDialog.getByLabel("Title", {exact: true}).waitFor();
    assert.equal(await missionDialog.getByLabel("Title", {exact: true}).inputValue(), "Derivative estimate", "Refresh discarded the mission draft");
    missionConflict = true;
    await missionDialog.getByRole("button", {name: "Save mission", exact: true}).click();
    await missionDialog.getByText("The saved mission changed. Your draft is retained.", {exact: true}).waitFor();
    assert.equal(await missionDialog.getByLabel("Title", {exact: true}).inputValue(), "Derivative estimate");
    await missionDialog.getByRole("button", {name: "Keep my draft against revision 2", exact: true}).click();
    await missionDialog.getByLabel("Parent mission", {exact: true}).selectOption("");
    await missionDialog.getByRole("button", {name: "Save mission", exact: true}).click();
    await missionDialog.waitFor({state: "hidden"});
    await tab.getByRole("button", {name: "Preview Derivative estimate", exact: true}).waitFor();
    assert.equal(missions[1].parent_id, null);
    await tab.getByRole("button", {name: `Create child of ${run.title}`, exact: true}).click();
    missionDialog = tab.getByRole("dialog", {name: "New mission", exact: true});
    await missionDialog.getByLabel("Title", {exact: true}).fill("Bounded child mission");
    await missionDialog.getByLabel("Objective", {exact: true}).fill("Prove the bounded child statement.");
    await missionDialog.getByLabel("Acceptance criteria", {exact: true}).fill("A checked proof of the child statement.");
    await missionDialog.getByLabel("Delegation rationale", {exact: true}).fill("This child supplies the parent's bounded estimate.");
    assert.equal(await missionDialog.getByLabel("Parent mission", {exact: true}).inputValue(), "mission-1");
    await missionDialog.getByRole("button", {name: "Create mission", exact: true}).click();
    await missionDialog.waitFor({state: "hidden"});
    await tab.getByRole("button", {name: "Preview Bounded child mission", exact: true}).waitFor();
    await tab.screenshot({path: path.join(artifacts, "mission-hierarchy-desktop.png"), fullPage: true});
    await tab
      .getByRole("button", { name: "Back to projects", exact: true })
      .click();
    for (const name of ["Execution hosts", "Agents", "Accounts"]) {
      await tab.getByRole("button", { name, exact: true }).click();
      await tab.getByRole("heading", { name, exact: true }).waitFor();
      assert.equal(
        await tab
          .locator("body")
          .evaluate((body) => body.scrollWidth <= innerWidth),
        true,
        `${name} overflows desktop`,
      );
    }
    for (const name of ["Forge", "Zulip"]) {
      await tab.getByRole("button", { name, exact: true }).click();
      await tab
        .frameLocator(`iframe[title="${name}"]`)
        .getByRole("heading")
        .waitFor();
    }
    await tab.getByRole("button", { name: "Activity", exact: true }).click();
    const cancelledRun = tab.locator(".activity-run-row").filter({hasText: "Improve the library"});
    await cancelledRun.getByLabel("Phase: Post-processing", {exact: true}).waitFor();
    assert.match(await cancelledRun.innerText(), /cancelled/i);
    await cancelledRun.click();
    await tab.locator(".activity-run-heading").getByLabel("Phase: Post-processing", {exact: true}).waitFor();
    assert.match(await tab.locator(".activity-heading-actions").innerText(), /cancelled/i);
    assert.equal(await tab.getByRole("button", {name: "Cancel run", exact: true}).count(), 0);
    await tab.screenshot({path: path.join(artifacts, "cancelled-run-phase-desktop.png"), fullPage: true});
    await tab.goBack();
    await tab.locator(".activity-run-row").filter({hasText: "Improve the library"}).waitFor();
    assert.equal(new URL(tab.url()).searchParams.get("run"), null, "Back should return to the Activity list");
    await tab
      .locator(".activity-run-row")
      .filter({ hasText: run.title })
      .click();
    await tab.locator(".activity-run-heading").getByLabel("Phase: Main formalization", {exact: true}).waitFor();
    await tab
      .locator(".activity-session-select")
      .filter({ hasText: session.title })
      .click();
    await tab
      .getByText("The derivative lemma now elaborates.", { exact: true })
      .first()
      .waitFor();
    assert.equal(await tab.locator(".activity-session-select.native-subagent").count(), 0);
    const subagents = tab.locator(".activity-subagents");
    assert.equal(await subagents.locator("summary").count(), 2);
    await subagents.getByText("mathematical fidelity", {exact: true}).click();
    await subagents.getByText("Verify the statement against the original mathematics.").waitFor();
    await subagents.getByText("check shared definitions", {exact: true}).click();
    await subagents.getByText("Compare the shared definitions and suggest a smaller reusable interface.").waitFor();
    await tab.screenshot({path: path.join(artifacts, "subagent-descriptions-desktop.png"), fullPage: true});
    await tab.getByRole("tab", { name: /Executions/ }).click();
    await tab.getByText("Execution 2", { exact: true }).waitFor();
    await tab.getByRole("tab", { name: /Reports/ }).click();
    await tab
      .getByText("The derivative bound is established.", { exact: true })
      .waitFor();
    await tab.getByRole("tab", { name: "Events", exact: true }).click();
    const queue = tab.getByLabel("Queued sessions", {exact: true});
    assert.deepEqual(await queue.locator(".activity-queue-session strong").allTextContents(), ["#501", "#502"]);
    const beforeHover = counts.get("/api/v3/dashboard/activity/sessions/assignment-501") || 0;
    await queue.locator(".activity-queue-session").first().hover();
    await queue.getByRole("tooltip").filter({hasText: "Session #500 is completed or failed or cancelled"}).waitFor({state: "visible"});
    assert.equal(counts.get("/api/v3/dashboard/activity/sessions/assignment-501") || 0, beforeHover, "Hover fetched session details");
    assert.equal(await queue.getByRole("tooltip").filter({hasText: "R12/A500 is using this workspace"}).isVisible(), true);
    await queue.locator(".activity-queue-session").first().focus();
    await tab.locator("h1").hover();
    assert.equal(await queue.getByRole("tooltip").filter({hasText: "R12/A500 is using this workspace"}).isVisible(), true, "Keyboard focus hid queue conditions");
    assert.ok(await tab.getByText("waiting to resume", {exact: true}).count() > 0);
    await tab.screenshot({
      path: path.join(artifacts, "activity-queue-desktop.png"),
      fullPage: true,
    });
    await tab
      .locator(".activity-session-select")
      .filter({ hasText: secondQueued.title })
      .click();
    await tab.getByText("Start conditions", {exact: true}).waitFor();
    assert.equal(await tab.locator(".activity-configuration").getByText("Ready for a slot", {exact: true}).count(), 1);
    assert.equal((await tab.locator(".activity-configuration").innerText()).includes('{"version"'), false);
    await tab.screenshot({path: path.join(artifacts, "activity-conditions-desktop.png"), fullPage: true});
    await tab
      .getByRole("combobox", { name: "Queue position" })
      .selectOption("first");
    await tab.waitForFunction(() =>
      document
        .querySelector(".activity-configuration")
        ?.textContent.includes("Position 1"),
    );
    assert.equal(secondQueued.queue_position, 1);
    assert.deepEqual(await queue.locator(".activity-queue-session strong").allTextContents(), ["#502", "#501"]);
    assert.equal(commandCount, 1);
    await tab
      .locator(".activity-session-select")
      .filter({ hasText: session.title })
      .click();
    await tab.locator(".activity-session h2").filter({hasText: session.title}).waitFor();
    const rootReads = counts.get("/api/v3/dashboard/activity/sessions/assignment-500") || 0;
    await queue.locator(".activity-queue-session").first().click();
    await tab.locator(".activity-session h2").filter({hasText: secondQueued.title}).waitFor();
    await tab.locator(".activity-session-select").filter({hasText: session.title}).click();
    await tab.locator(".activity-session h2").filter({hasText: session.title}).waitFor();
    assert.equal(counts.get("/api/v3/dashboard/activity/sessions/assignment-500") || 0, rootReads, "Returning to a fresh session refetched its transcript");
    assert.equal(counts.get("/api/v3/runs/run-12") || 0, 0, "Shell fetched a duplicate run record");
    await tab.getByRole("button", {name: "Refresh activity", exact: true}).click();
    await tab.waitForTimeout(300);
    assert.ok((counts.get("/api/v3/dashboard/activity/sessions/assignment-500") || 0) > rootReads, "Explicit refresh reused a stale transcript");
    const route = "/api/v3/dashboard/activity/runs/run-12",
      before = counts.get(route) || 0;
    emit(10);
    await tab.waitForTimeout(1300);
    const once = counts.get(route);
    assert.ok(once > before, "SSE did not refresh activity");
    emit(10);
    await tab.waitForTimeout(400);
    assert.equal(
      counts.get(route),
      once,
      "Duplicate SSE event refreshed activity",
    );
    const burstBefore = counts.get(route) || 0;
    for (let sequence = 11; sequence <= 30; sequence++) emit(sequence);
    await tab.waitForTimeout(1500);
    assert.equal(counts.get(route) - burstBefore, 3, "Activity event burst repeated a summary, metrics or queue read");
    const slowFailures = [];
    const recordFailure = request => {
      if (new URL(request.url()).pathname === route) slowFailures.push(request.failure());
    };
    tab.on("requestfailed", recordFailure);
    let slowCompleted = 0;
    const recordResponse = response => {
      if (new URL(response.url()).pathname === route && response.status() === 200) slowCompleted++;
    };
    tab.on("response", recordResponse);
    await tab.route(`**${route}?*`, async intercepted => {
      await new Promise(resolve => setTimeout(resolve, 2500));
      await intercepted.continue().catch(() => {});
    });
    await tab.getByRole("button", {name: "Refresh activity", exact: true}).click();
    for (let sequence = 31; sequence <= 35; sequence++) {
      emit(sequence);
      await tab.waitForTimeout(700);
    }
    await tab.waitForTimeout(4000);
    await tab.unroute(`**${route}?*`);
    tab.off("requestfailed", recordFailure);
    tab.off("response", recordResponse);
    assert.equal(slowFailures.length, 0, "Live updates cancelled slow Activity reads");
    assert.ok(slowCompleted > 0, "Slow reads never completed during live updates");
    const beforeGap = counts.get(route) || 0;
    for (const stream of streams)
      stream.write(
        'event: gap\ndata: {"sequence":40,"gap":true,"resources":[]}\n\n',
      );
    await tab.waitForTimeout(1300);
    assert.ok(
      counts.get(route) > beforeGap,
      "Replay gap did not refresh activity",
    );
    session.status = "failed";
    session.can_resume = true;
    session.revision++;
    emit(41);
    await tab
      .getByRole("button", { name: "Resume session", exact: true })
      .waitFor();
    outage = true;
    for (const stream of streams) stream.destroy();
    await tab.getByText("Reconnecting", { exact: true }).waitFor();
    await tab.getByRole("button", { name: "Refresh", exact: true }).click();
    await tab
      .getByText("Fixture control plane unavailable", { exact: false })
      .first()
      .waitFor();
    assert.equal(
      await tab
        .locator(".activity-session-select")
        .filter({ hasText: session.title })
        .count(),
      1,
      "Outage discarded cached activity",
    );
    assert.equal(
      await tab
        .getByRole("button", { name: "Resume session", exact: true })
        .count(),
      0,
      "Outage permitted mutation",
    );
    outage = false;
    await tab
      .getByText("Connected", { exact: true })
      .waitFor({ timeout: 15000 });
    await tab
      .getByRole("button", { name: "Resume session", exact: true })
      .waitFor();
    dropCommandAck = true;
    const beforeCommands = commandCount;
    tab.once("dialog", dialog => dialog.accept("The provider connection has recovered."));
    await tab
      .getByRole("button", { name: "Resume session", exact: true })
      .click();
    await tab.waitForFunction(
      () => sessionStorage.getItem("horizon.pipeline.pending-command") === null,
    );
    assert.equal(
      commandCount,
      beforeCommands + 1,
      "Lost acknowledgement repeated mutation",
    );
    await tab.locator(".activity-session-heading .activity-status").filter({hasText: "queued"}).waitFor();
    session.status = "failed";
    session.can_resume = true;
    session.revision++;
    emit(101);
    await tab
      .getByRole("button", { name: "Resume session", exact: true })
      .waitFor();
    await tab.route("**/api/v3/commands", (route) => route.abort("failed"));
    const beforeDroppedRequest = commandCount;
    tab.once("dialog", dialog => dialog.accept("The provider connection has recovered."));
    await tab
      .getByRole("button", { name: "Resume session", exact: true })
      .click();
    await tab
      .getByRole("button", { name: "Retry same operation", exact: true })
      .waitFor();
    const saved = await tab.evaluate(() =>
      JSON.parse(sessionStorage.getItem("horizon.pipeline.pending-command")),
    );
    await tab.reload();
    await tab.getByText("Connected", { exact: true }).waitFor();
    await tab
      .getByRole("button", { name: "Retry same operation", exact: true })
      .waitFor();
    assert.equal(
      commandCount,
      beforeDroppedRequest,
      "Reload replayed a command automatically",
    );
    await tab.unroute("**/api/v3/commands");
    await tab
      .getByRole("button", { name: "Retry same operation", exact: true })
      .click();
    await tab.waitForFunction(
      () => sessionStorage.getItem("horizon.pipeline.pending-command") === null,
    );
    assert.ok(
      operations.has(saved.id),
      "Manual retry changed the operation identity",
    );
    assert.equal(commandCount, beforeDroppedRequest + 1);
    const progressive = await browser.newPage({viewport: {width: 1440, height: 1000}});
    const detailReads = [];
    let releaseDetails;
    const heldDetails = new Promise(resolve => {releaseDetails = resolve;});
    await progressive.route("**/api/v3/dashboard/activity/**", async intercepted => {
      const target = new URL(intercepted.request().url());
      detailReads.push(target);
      if (target.searchParams.has("view") || target.pathname.endsWith("/sessions/assignment-500")) await heldDetails;
      await intercepted.continue().catch(() => {});
    });
    try {
      await progressive.goto(`${base}/pipeline?tab=activity&run=run-12&session=assignment-500`);
      await progressive.locator(".activity-session h2").filter({hasText: session.title}).waitFor();
      await progressive.getByText("The derivative lemma now elaborates.", {exact: true}).first().waitFor();
      assert.ok(await progressive.getByLabel("Agent tree", {exact: true}).isVisible(), "Detail delay hid the tree");
      assert.equal(detailReads.some(target => target.searchParams.get("view") === "reports"), false, "Reports loaded before opening their tab");
      assert.equal(detailReads.some(target => target.pathname.endsWith("/logs")), false, "Logs loaded before opening their tab");
      assert.equal(detailReads.find(target => target.pathname.endsWith("/events"))?.searchParams.get("limit"), "15");
      await progressive.screenshot({path: path.join(artifacts, "activity-progressive-desktop.png"), fullPage: true});
      releaseDetails();
      await progressive.locator(".activity-subagents summary").first().waitFor();
      await progressive.getByRole("tab", {name: /Reports/}).click();
      await progressive.getByText("The derivative bound is established.", {exact: true}).waitFor();
      const ledger = progressive.getByRole("region", {name: "Goal ledger"});
      await ledger.locator("strong").filter({hasText: "endpoint"}).waitFor();
      assert.equal(await ledger.getByText("Delegated", {exact: true}).isVisible(), false);
      await ledger.getByText("Settled history (1)", {exact: true}).click();
      await ledger.getByText("Delegated", {exact: true}).waitFor();
      assert.match(await ledger.getByRole("link", {name: /Delegated session|#501/}).getAttribute("href"), /session=assignment-501/);
      assert.equal(await ledger.getByText("Check the", {exact: false}).isVisible(), false);
      await ledger.getByText("Comments (1)", {exact: true}).click();
      await ledger.locator("strong").filter({hasText: "boundary case"}).waitFor();
    } finally {
      releaseDetails();
      await progressive.close();
    }
    assert.equal(
      [...counts.keys()].some((key) => key.startsWith("/api/v2")),
      false,
    );
    assert.deepEqual(unknownRoutes, []);
    assert.deepEqual(errors, []);
    assert.equal(
      await tab
        .locator("body")
        .evaluate((body) => body.scrollWidth <= innerWidth),
      true,
      "Activity overflows desktop",
    );
    console.log(
      JSON.stringify(
        {
          viewport: { width: 1440, height: 1000 },
          initialRequests: startup.length,
          initialBytes: startup.reduce((sum, item) => sum + item.bytes, 0),
          commands: commandCount,
          errors,
        },
        null,
        2,
      ),
    );
  } finally {
    await browser.close();
    for (const stream of streams) stream.destroy();
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
