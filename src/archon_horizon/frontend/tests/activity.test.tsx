import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import { activityDuration, activityLink, AdmissionDetails, currentMainSession, EventTimeline, groupActivityEvents, runSlotCapacity, sessionCounts, sessionStatus, sessionTree, type ActivityEvent, type ActivitySession } from "../src/components/ActivityTab";
import { capabilityHref } from "../src/utils/capabilityLinks";
import { activityContextNodes, activityEventDetails, activityEventTitle, activityNodes, activityNoticeSummary, activityNotices, gitTransferBursts } from "../src/utils/activityPresentation";
import { activityCommand, type ActivityRevision } from "../src/pipeline/DesktopActivity";

const session = (id: string, parent_session_id?: string): ActivitySession => ({
  id, parent_session_id, run_id: "run", label: id, title: id, role: "worker", status: "succeeded",
  created_at: "2026-09-07T03:00:00Z", usage: { tokens_in: null, tokens_out: null, cost_usd: null },
  elapsed_seconds: 0, agent_seconds: 0, attempt_count: 1, models: [], skills: [], context: {},
});

const event = (id: string, kind: string, attempt_id = "attempt-1", session_id = "session-1"): ActivityEvent => ({
  id, kind, title: id, attempt_id, session_id, created_at: "2026-09-07T03:00:00Z", data: {}, links: [],
});

test("retained sessions distinguish waiting to resume from finished work and display physical blockers", () => {
  const retained = {...session("18"), status: "queued", context: {retained_continuation: true}};
  assert.equal(sessionStatus(retained), "waiting to resume");
  assert.equal(sessionStatus({...retained, status: "succeeded"}), "succeeded");
  assert.equal(sessionStatus({...retained, context: {}}), "queued");
  const html = renderToStaticMarkup(<AdmissionDetails admission={{state: "waiting", summary: "Session #27 is completed",
    reason: "Retained workspace on worker-1 is occupied by session R3/A6"}} />);
  assert.match(html, /Retained workspace on worker-1 is occupied by session R3\/A6/);
  assert.doesNotMatch(html, /Ready for a slot/);
});

test("desktop commands retain observed revisions and queue positions without granting native children slots", () => {
  const queued = (id: string, revision: number, queue_position: number, native = false) => ({id, revision, queue_position, native, status: "queued"});
  const a = queued("a", 12, 30), b = queued("b", 19, 10), child = queued("child", 2, 15, true);
  const records = new Map<string, ActivityRevision>([["run", {id: "run", revision: 7, sessions: [a, child, b]}], [a.id, a], [b.id, b], [child.id, child]]);
  const queue = (id: string, priority: string) => activityCommand("/dashboard/activity/runs/run/queue", {method: "PATCH", body: JSON.stringify({session_id: id, priority})}, records, true);
  assert.deepEqual(queue("a", "up"), {operation: "move_before", target_id: "a", expected_revision: 12, args: {other_id: "b"}});
  assert.deepEqual(queue("b", "down"), {operation: "move_after", target_id: "b", expected_revision: 19, args: {other_id: "a"}});
  assert.equal(queue("b", "up"), null);
  assert.equal(queue("a", "last"), null);
  assert.throws(() => queue("child", "first"), /Refresh activity/);
  assert.throws(() => queue("a", "urgent"), /Unsupported queue position/);
  assert.deepEqual(activityCommand("/dashboard/activity/runs/run/cancel", {method: "POST"}, records, true),
    {operation: "cancel_run", target_id: "run", expected_revision: 7, args: {}});
  assert.deepEqual(activityCommand("/dashboard/activity/sessions/a/resume", {method: "POST"}, records, true),
    {operation: "retry_assignment", target_id: "a", expected_revision: 12, args: {}});
  assert.throws(() => activityCommand("/dashboard/activity/runs/run/cancel", {method: "POST"}, records, false), /cannot change/);
  assert.throws(() => activityCommand("/api/v2/activity/runs/run/cancel", {method: "POST"}, records, true), /Unsupported/);
  assert.throws(() => activityCommand("/dashboard/activity/sessions/a/cancel", {method: "POST"}, records, true), /Unsupported/);
  assert.throws(() => activityCommand("/dashboard/activity/runs/missing/cancel", {method: "POST"}, records, true), /Refresh activity/);
});

test("single provider tool output stays collapsed and preformatted in the original timeline", () => {
  const html = renderToStaticMarkup(<EventTimeline events={[{...event("tool", "provider.tool"), title: "Read Main.lean", data: {detail: "command: cat Main.lean\noutput: theorem example"}}]} />);
  assert.match(html, /<details class="activity-hook-raw"><summary>/);
  assert.match(html, /<pre>command: cat Main.lean\noutput: theorem example<\/pre>/);
  assert.doesNotMatch(html, /<details[^>]*open|<p>command:/);
});


test("session hierarchy preserves descendants and safely displays incomplete imported history", () => {
  const sessions = [session("root"), session("child", "root"), session("grandchild", "child"), session("orphan", "missing"), session("cycle-a", "cycle-b"), session("cycle-b", "cycle-a")];
  const rows = sessionTree(sessions);
  assert.deepEqual(rows.map(({ session, depth }) => [session.id, depth]), [["root", 0], ["child", 1], ["grandchild", 2], ["orphan", 0], ["cycle-a", 0], ["cycle-b", 1]]);
  assert.equal(rows[0].hasChildren, true);
  assert.equal(rows[2].hasChildren, false);
  assert.deepEqual(sessionTree(sessions, new Set(["root"])).map(({ session }) => session.id), ["root", "orphan", "cycle-a", "cycle-b"]);
});

test("opening a run selects its current main session across delegated branches", () => {
  const sessions = [{ ...session("first"), label: "1" }, { ...session("second"), label: "2" },
    { ...session("latest"), label: "10", status: "running" },
    { ...session("child", "first"), label: "1.1", status: "running" }];
  assert.equal(currentMainSession(sessions).id, "latest");
  assert.deepEqual(sessionTree(sessions).map(({ session }) => session.id), ["first", "child", "second", "latest"]);
  assert.equal(currentMainSession(sessions.map(item => ({ ...item, status: "succeeded" }))).id, "latest");
});

test("agent tree can hide finished sessions while occupancy counts stay compact", () => {
  const sessions = [
    { ...session("root"), status: "running" },
    { ...session("child", "root"), status: "queued" },
    { ...session("done", "root"), status: "succeeded" },
    { ...session("waiting", "root"), status: "waiting" },
  ];
  assert.deepEqual(sessionCounts(sessions), { running: 1, queued: 2, total: 4 });
  assert.deepEqual(sessionTree(sessions, new Set(), "active").map(({ session }) => session.id), ["root", "child", "waiting"]);
});

test("occupancy capacity is the current run's available scheduler slots", () => {
  assert.equal(runSlotCapacity({
    default_harness: "codex-astra",
    hosts: Object.fromEntries(["machine-0", "tencent-sv-1", "tencent-sv-2", "tencent-sv-3", "tencent-sv-4"]
      .map((id) => [id, { harness_limits: { "codex-astra": 20 } }])),
  }), 100);
  assert.equal(runSlotCapacity({
    max_concurrent_runs: 40,
    hosts: {
      "worker-a": { harness_limits: { "codex-astra": 10, claude: 0 } },
      "worker-b": { max_concurrent_runs: 6, harness_limits: { "codex-astra": 10 } },
    },
  }), 16);
  assert.equal(runSlotCapacity({ max_concurrent_runs: 8 }), 8);
  assert.equal(runSlotCapacity({ hosts: { "worker-a": { harness_limits: { "codex-astra": 0 } } } }), undefined);
  assert.equal(runSlotCapacity({}), undefined);
});

test("session hierarchy orders newer sibling sessions first", () => {
  const sessions = [
    { ...session("old"), created_at: "2026-09-07T03:00:00Z" },
    { ...session("new"), created_at: "2026-09-08T03:00:00Z" },
    { ...session("old-child", "old"), created_at: "2026-09-09T03:00:00Z" },
  ];
  assert.deepEqual(sessionTree(sessions).map(({ session }) => session.id), ["new", "old", "old-child"]);
});

test("event references stay clickable while untrusted protocols and markup are inert", () => {
  const html = renderToStaticMarkup(<EventTimeline events={[{
    id: "event-1", kind: "objective.pr.opened", title: "<script>Unsafe</script>", created_at: "2026-09-07T03:00:00Z",
    data: { summary: "<img src=x onerror=alert(1)>" }, links: [
      { label: "Objective PR #7", url: "/api/v2/forge/web/math/objective/pulls/7" },
      { label: "Unsafe", url: "javascript:alert(1)" },
      { label: "Reference", url: "https://example.org/source" },
    ],
  }]} />);
  assert.match(html, /href="\/api\/v2\/forge\/web\/math\/objective\/pulls\/7"/);
  assert.match(html, /dateTime="2026-09-07T03:00:00Z"/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /href="javascript:|<script>|<img/);
  assert.equal(activityLink("data:text/html,<script>bad</script>"), undefined);
  assert.equal(activityLink("?tab=activity&run=123"), "?tab=activity&run=123");
});

test("visible agent messages remain standalone activity with attribution and bounded state", () => {
  const parent = { ...event("parent-message", "agent.message"), title: "Agent message",
    data: { markdown: "Checking **the next case**.\n\n<script>unsafe</script>" } };
  const child = { ...event("child-message", "agent.message"), title: "translation",
    data: { markdown: "Found `Nat.succ`.", external_id: "native-child", agent: "translation", truncated: true } };
  const groups = groupActivityEvents([parent, child]);
  assert.equal(groups.length, 2);
  assert.equal(groups[0].category.label, "Agent messages");
  assert.equal(activityEventTitle(parent), "Agent said");
  assert.equal(activityEventTitle(child), "translation said");
  const html = renderToStaticMarkup(<EventTimeline events={[parent, child]} projectId="p" />);
  assert.match(html, /Agent said/);
  assert.match(html, /translation said/);
  assert.match(html, /Checking \*\*the next case\*\*/);
  assert.match(html, /Found `Nat\.succ`/);
  assert.match(html, /Truncated/);
  assert.doesNotMatch(html, /<script>unsafe/);
});

test("duration labels distinguish absent metrics from measured zero", () => {
  assert.equal(activityDuration(undefined), "Not recorded");
  assert.equal(activityDuration(Number.NaN), "Not recorded");
  assert.equal(activityDuration(0), "0s");
  assert.equal(activityDuration(125), "2m 5s");
  assert.equal(activityDuration(7260), "2h 1m");
});

test("consecutive categories merge without reordering intervening events", () => {
  const events = [event("4", "graph.node.updated"), event("3", "graph.node.created"), event("2", "zulip.discussion.read"), event("1", "graph.node.updated")];
  const groups = groupActivityEvents(events);
  assert.deepEqual(groups.map(group => group.events.map(event => event.id)), [["4", "3"], ["2"], ["1"]]);
  assert.equal(groups[0].category.label, "Graph nodes");
  assert.equal(groupActivityEvents([event("5", "graph.node.updated"), ...events])[0].id, groups[0].id);
});

test("grouping preserves attempt, session, and failure boundaries", () => {
  const events = [event("4", "graph.node.updated"), event("3", "graph.node.updated", "attempt-2"),
    event("2", "graph.node.updated", "attempt-2", "session-2"), event("1", "session.failed"), event("0", "session.failed"),
    { ...event("git-failed", "forge.git.push"), data: { transfer_state: "failed", repository_kind: "workspace" } },
    { ...event("git-stopped", "forge.git.push"), data: { transfer_state: "interrupted", repository_kind: "workspace" } }];
  assert.equal(groupActivityEvents(events).length, events.length);
});

test("collapsed categories retain their range without mounting hidden reference lookups", () => {
  const newest = { ...event("new", "graph.node.updated"), created_at: "2026-09-07T03:01:00Z",
    data: { summary: "Improved the estimate" }, links: [{ label: "Bound", url: "?project=p&node=bound" }] };
  const oldest = { ...event("old", "graph.node.created"), links: [{ label: "Lemma", url: "?project=p&node=lemma" }] };
  const html = renderToStaticMarkup(<EventTimeline events={[newest, oldest]} />);
  assert.match(html, /<details class="activity-event-group">/);
  assert.match(html, /Graph nodes/);
  assert.match(html, /2 events/);
  for (const item of [newest, oldest]) {
    assert(html.includes(`dateTime="${item.created_at}"`));
    assert(!html.includes(item.links[0].url.replaceAll("&", "&amp;")));
  }
  assert.doesNotMatch(html, /Improved the estimate/);
  assert.equal(groupActivityEvents([newest, oldest])[0].events[0].data.summary, "Improved the estimate");
});

test("successful hook delivery is a standalone notification, not a collapsed hook group", () => {
  const notice = { ...event("hook", "hook.delivered"), title: "Coordination hook delivered 2 notices",
    data: { hook: "PostToolUse", count: 2, kinds: ["mission.dispatched", "zulip.message"], summary: "Narrow chart lemma",
      notices: [{ kind: "mission.dispatched", title: "Produce chart lemma", priority: 0, url: "/missions/chart" },
        { kind: "zulip.message", title: "Chart pullback", priority: 1, url: "/zulip/topic" }] } };
  assert.equal(activityEventTitle(notice), "2 notifications delivered");
  assert.equal(groupActivityEvents([notice, { ...notice, id: "hook-2" }]).length, 2);
  const html = renderToStaticMarkup(<EventTimeline events={[notice]} />);
  assert.match(html, /1 mission dispatch, 1 Zulip mention/);
  assert.match(html, /Mission dispatched/);
  assert.match(html, /Produce chart lemma/);
  assert.match(html, /Mentioned in Zulip/);
  assert.match(html, /Chart pullback/);
  assert.doesNotMatch(html, /Narrow chart lemma|PostToolUse|mission\.dispatched/);
  assert.match(html, /href="\/zulip\/topic"/);
});

test("native hook output and the report gate render their supplied context as hook events", () => {
  const output = { ...event("hook-output", "hook.output"), data: {
    hook: "SessionStart", context_truncated: true,
    additional_context: "Horizon session briefing (live coordination state):\n{\"mission\":{\"original_\"",
    briefing: {
      project_id: "poincare-conjecture",
      summary: "2 sessions running; 1 queued; 3 eligible slots available; 2 Roadmap PRs open.",
      sessions: { running: 2, queued: 1 }, parallelism: { available: 3, max_parallelism: 6, budget_remaining: 7 },
      pull_requests: { roadmap: { open: 2 }, awaiting_review: 3, awaiting_author: 1 },
      mission: { id: "m07", title: "Construct the geometric limit", content: "Finish the checked assembly.",
        status: "active", revision: 4, node_count: 2, node_attempt_count: 1 },
      active_sessions: [{ session_id: "peer-session", run_id: "run-2", mission_id: "review-curvature", status: "running", scope: "project" }],
      active_sessions_total: 1,
      refill: { remaining_target: 1, dispatch_target: 2, reason: "underutilized_capacity" },
      frontier_url: "/api/v2/frontier", maintenance_url: "/api/v2/maintenance",
    },
  } };
  const gate = { ...event("gate", "report.stop_gate"), title: "Session must continue",
    data: { may_stop: false, reasons: ["A reachable obligation remains."] } };
  assert.equal(groupActivityEvents([output, gate])[0].category.label, "Hooks");
  assert.equal(activityEventTitle(output), "SessionStart hook supplied context");
  const html = renderToStaticMarkup(<EventTimeline events={[output, gate]} />);
  assert.match(html, /2 sessions running/);
  assert.match(html, /Slots available[\s\S]*3/);
  assert.match(html, /Roadmap PRs[\s\S]*2/);
  assert.match(html, /Construct the geometric limit/);
  assert.match(html, /project=poincare-conjecture&amp;project_view=missions&amp;mission=m07/);
  assert.match(html, /Finish the checked assembly/);
  assert.match(html, /Other active sessions[\s\S]*review-curvature[\s\S]*Other run/);
  assert.match(html, /Queue refill[\s\S]*1 of 2 admissions remaining/);
  assert.match(html, /Open frontier/);
  assert.match(html, /Raw hook output/);
  assert.match(html, /Raw hook output truncated/);
  assert.match(html, /A reachable obligation remains/);
  assert(html.includes(capabilityHref("hooks", "SessionStart", "session-1").replaceAll("&", "&amp;")));
  assert(html.includes(capabilityHref("hooks", "Report stop gate", "session-1").replaceAll("&", "&amp;")));
});




test("notification counts distinguish mentions, private messages and session results from mission completion", () => {
  const kinds = ["mission.dispatched", "mission.finished.succeeded", "mission.finished.failed", "mission.finished.cancelled", "zulip.message", "zulip.private"];
  const notice = { ...event("types", "hook.delivered"), data: { count: 12,
    notice_counts: kinds.map(kind => ({ kind, priority: 0, count: 2 })),
    notices: kinds.map(kind => ({ kind, title: kind === "zulip.private" ? "Private message" : "Title", priority: 0, url: "javascript:alert(1)" })) } };
  assert.equal(activityNoticeSummary(notice), "2 mission dispatches, 2 successful sessions, 2 failed sessions, 2 cancelled sessions, 2 Zulip messages, 2 Zulip direct messages");
  const html = renderToStaticMarkup(<EventTimeline events={[notice]} />);
  for (const label of ["Mission dispatched", "Session succeeded", "Session failed", "Session cancelled", "New Zulip message", "Zulip direct message"]) assert(html.includes(label));
  assert.doesNotMatch(html, /Mission finished|Mission completed|javascript:|href=/);
});

test("bounded and unfamiliar notice metadata stays readable", () => {
  const notice = { ...event("bounded", "hook.delivered"), data: { count: 100,
    notice_counts: [{ kind: "zulip.message", priority: 1, count: 100 }], notices_omitted: 99,
    notices: [null, false, [], { kind: "zulip.message", title: "Route <script>", priority: 1, stream: "Geometry" }] } };
  const html = renderToStaticMarkup(<EventTimeline events={[notice]} />);
  assert.match(html, /100 Zulip mentions/);
  assert.match(html, /99 more notifications/);
  assert.match(html, /Route &lt;script&gt;/);
  assert.match(html, /Geometry/);
  assert.equal(activityNotices(notice).length, 1);
  assert.equal(activityNoticeSummary({ ...notice, data: { notice_counts: [{kind: "custom.update", count: 1}] } }), "1 custom update notice");
});

test("old capability events link to the session snapshot without catalog requests", () => {
  const skill = { ...event("skill", "skill.loaded"), data: { skill: "lean-check" } };
  const child = { ...event("delegate", "session.delegated"), data: { agent: "proof-reviewer", capability_session_id: "child" } };
  const explicit = { ...event("mcp", "mcp.tool.called"), data: { server: "lean & search" },
    links: [{ label: "lean & search", url: capabilityHref("mcp", "lean & search", "session-1") }] };
  const html = renderToStaticMarkup(<EventTimeline events={[skill, child, explicit]} />);
  assert(html.includes(capabilityHref("skills", "lean-check", "session-1").replaceAll("&", "&amp;")));
  assert(html.includes(capabilityHref("agents", "proof-reviewer", "child").replaceAll("&", "&amp;")));
  assert.equal(html.split('href="' + explicit.links[0].url.replaceAll("&", "&amp;") + '"').length - 1, 1);
});

test("subagent launches and finishes stay visible with descriptions and outcomes", () => {
  const data = { title: "translation", description: "Translate <Lean> independently.", status: "running" };
  const launch = { ...event("launch", "subagent.started"), data };
  const second = { ...launch, id: "second", data: { ...data, title: "review" } };
  const finish = { ...event("finish", "subagent.finished"), data: { ...data, status: "failed" } };
  const groups = groupActivityEvents([finish, launch, second]);
  assert.equal(groups.length, 3);
  assert.equal(groups[0].category.label, "Attention");
  const html = renderToStaticMarkup(<EventTimeline events={[finish, launch, second]} />);
  assert.match(html, /Subagent launched: translation/);
  assert.match(html, /Subagent finished: translation/);
  assert.match(html, /Translate &lt;Lean&gt; independently/);
  assert.match(html, /<dt>Status<\/dt><dd>failed/);
  assert.doesNotMatch(html, /activity-event-group /);
  assert.match(activityEventTitle({ ...launch, kind: "session.delegated" }), /Delegated session queued/);
  assert.match(activityEventTitle({ ...launch, kind: "session.delegated", data: { ...data, external_id: "native" } }), /Delegated session launched/);
  assert.equal(activityEventTitle({ ...launch, data: { ...data, native: false, lifecycle: "delegated_session" } }), "Delegated session started: translation");
  assert.equal(activityEventTitle({ ...launch, data: { ...data, native: true } }), "Native subagent launched: translation");
  assert.equal(groupActivityEvents([{ ...launch, data: { ...data, native: false, lifecycle: "delegated_session" } }])[0].category.label, "Delegated sessions");
  assert.equal(groupActivityEvents([{ ...launch, data: { ...data, native: true } }])[0].category.label, "Subagents");
  assert.equal(groupActivityEvents([{ ...launch, kind: "session.delegated", data: { ...data, native: true } }])[0].category.label, "Subagents");
  assert.match(activityEventDetails({ ...launch, kind: "session.delegated", data: { ...data, native: false } }).map(item => item.value).join(" "), /Delegated Horizon session/);
});

test("search API and LeanSearch MCP calls show the query", () => {
  const workspace = { ...event("search", "search.workspace"), data: { scope: "workspace", mode: "text", query: "compact image", hit_count: 3 } };
  const mcp = { ...event("mcp", "mcp.tool.called"), data: { server: "lean-lsp", tool: "lean_leansearch", query: "compact image" } };
  assert.equal(activityEventTitle(workspace), "Searched workspace (text): compact image");
  assert.equal(activityEventTitle({ ...event("search", "search.library.removed"), data: { name: "TauCeti" } }), "Removed search library: TauCeti");
  assert.equal(activityEventTitle(mcp), "MCP search: lean-lsp / lean_leansearch — compact image");
  assert.match(activityEventDetails(workspace).map(item => item.value).join(" "), /compact image/);
  assert.equal(groupActivityEvents([workspace, mcp])[0].category.label, "Search");
  assert.equal(groupActivityEvents([{ ...event("mcp", "mcp.tool.called"), data: { server: "lean-lsp", tool: "lean_goal" } }])[0].category.label, "MCP calls");
  const html = renderToStaticMarkup(<EventTimeline events={[workspace]} />);
  assert.match(html, /Searched workspace \(text\): compact image/);
  assert.match(html, /compact image/);
});

test("activity distinguishes Lake builds, file checks and LSP without trusting the old mode label", () => {
  const started = { ...event("check", "lean.check.started"), data: { mode: "lsp", command: ["lake", "env", "lean", "Main.lean"], cwd: "/work/Morgan Tian" } };
  assert.equal(activityEventTitle(started), "Lean file check started");
  assert.equal(activityEventTitle({ ...started, data: { mode: "lsp" } }), "Lean LSP started");
  const build = { ...started, data: { command: ["lake", "build", "MorganTianLib.Ch05"] } };
  assert.equal(activityEventTitle(build), "Lake build started");
  assert.equal(activityEventDetails(build)[0].value, "lake build MorganTianLib.Ch05");
  const failed = { ...build, kind: "lean.check.finished", data: { ...build.data, status: "timed_out", returncode: 124, duration_seconds: 1800 } };
  assert.equal(activityEventTitle(failed), "Lake build timed out");
  assert.equal(groupActivityEvents([failed])[0].category.label, "Attention");
  const html = renderToStaticMarkup(<EventTimeline events={[started, failed]} />);
  assert.match(html, /Lean file check started|Lake build timed out/);
  assert.match(html, /lake env lean Main.lean/);
  assert.match(html, /\/work\/Morgan Tian/);
});

test("mission context lists involved nodes without duplicating a dedicated node_id", () => {
  assert.deepEqual(activityContextNodes({ project_id: "p", node_id: "bound", node_ids: ["bound", "lemma", "lemma"] }),
    [{ project: "p", id: "bound" }, { project: "p", id: "lemma" }]);
});

test("node references are recovered from data and old links once, scoped to their project", () => {
  const node = { ...event("node", "graph.node.read"), data: { project_id: "p", target_id: "bound", node_ids: ["bound", "lemma"] }, links: [{ label: "Node", url: "/?project=p&node=bound" }] };
  assert.deepEqual(activityNodes(node), [{ project: "p", id: "bound" }, { project: "p", id: "lemma" }]);
  const html = renderToStaticMarkup(<EventTimeline events={[node]} />);
  assert.equal(html.split('href="?project=p&amp;node=bound"').length - 1, 1);
  const legacy = { ...event("list", "graph.node.read"), title: "Node read", data: { target_id: null } };
  assert.equal(activityEventTitle(legacy), "Node list read");
  assert.match(activityEventDetails(legacy)[0].value, /not recorded/);
  assert.deepEqual(activityNodes({ ...node, data: {}, links: [{ label: "External", url: "https://external.example/?project=p&node=bound" }] }), []);
});

test("repository transfers and mission groups have meaningful labels", () => {
  const transfer = { ...event("git", "forge.git.fetch"), data: { operation: "fetch", transfer_state: "completed", organization: "geometry", repository: "workspace" }, links: [{ label: "Repository", url: "/api/v2/forge/web/geometry/workspace" }] };
  const html = renderToStaticMarkup(<EventTimeline events={[transfer]} />);
  assert.match(html, /Git fetch:/);
  assert.match(html, /transfer completed/);
  assert.match(html, /geometry\/workspace/);
  assert.doesNotMatch(html, /Git pull|Git rebase/);
  assert.equal(groupActivityEvents([event("m", "graph.mission.read")])[0].category.label, "Missions");
});


test("Git checkpoints use compact commit links and retain details behind disclosure", () => {
  const checkpoint = { ...event("commit", "workspace.checkpoint"), title: "Add the reduced-volume definition", data: { commit: "4705cd751eaa699bfc288df71c55df8bf507adc2", branch: "main", files: ["Main.lean"], operations: ["commit", "rebase", "push"] }, links: [{ label: "Workspace commit", url: "/api/v2/forge/web/p/workspace/commit/4705cd751eaa699bfc288df71c55df8bf507adc2" }] };
  const html = renderToStaticMarkup(<EventTimeline events={[checkpoint]} />);
  assert.match(html, /Git push:/);
  assert.match(html, />4705cd751eaa</);
  assert.match(html, /Add the reduced-volume definition/);
  assert.match(html, /activity-git-details/);
  assert.match(html, /commit &gt; rebase &gt; push/);
  assert.doesNotMatch(html, /<dt>Commit|<dt>Branch/);
});

test("Git protocol bursts keep raw exchanges and never merge failure or session boundaries", () => {
  const transfer = { ...event("git", "forge.git.fetch"), data: { transfer_state: "completed", repository: "repo", http_status: 200 } };
  assert.deepEqual(activityEventDetails(transfer), []);
  const failed = { ...transfer, id: "failed", data: { ...transfer.data, http_status: 500, transfer_state: "failed" } };
  assert.equal(activityEventDetails(failed)[0].value, "500");
  const events = [transfer, { ...transfer, id: "2" }, { ...transfer, id: "3", session_id: "another" }, failed, { ...transfer, id: "4", data: { ...transfer.data, repository: "different" } }];
  assert.deepEqual(gitTransferBursts(events).map(burst => burst.map(item => item.id)), [["git", "2"], ["3"], ["failed"], ["4"]]);
  assert.equal(gitTransferBursts([transfer, { ...transfer, created_at: "2026-09-07T03:01:00Z" }]).length, 2);
});
