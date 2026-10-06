import assert from "node:assert/strict";
import { canonicalDashboardUrl, dashboardUrl, projectDashboardUrl } from "../src/utils/navigation";

const dirty = "https://horizon.test/?tab=zulip&kind=hooks&name=preflight&project=p&node=n&proposal=legacy&run=r#fragment";

assert.equal(dashboardUrl("zulip", dirty).href, "https://horizon.test/?tab=zulip");
assert.equal(dashboardUrl("search", dirty).href, "https://horizon.test/?tab=search");
assert.equal(dashboardUrl("accounts", dirty).href, "https://horizon.test/?tab=accounts");
assert.equal(dashboardUrl("projects", dirty).href, "https://horizon.test/?tab=projects");
assert.equal(canonicalDashboardUrl("zulip", dirty).href, "https://horizon.test/?tab=zulip");
assert.equal(canonicalDashboardUrl("agents", dirty).href, "https://horizon.test/?tab=agents&kind=hooks&name=preflight");
assert.equal(canonicalDashboardUrl("activity", dirty).href, "https://horizon.test/?tab=activity&run=r");
assert.equal(canonicalDashboardUrl("projects", dirty).href, "https://horizon.test/?tab=projects&project=p&node=n&run=r");

const nodeHome = "https://horizon.test/?tab=projects&project=p&project_view=node&node=alpha&objective=roadmap&pull=7";
assert.equal(projectDashboardUrl("p", "node-dag", {}, nodeHome).href,
  "https://horizon.test/?tab=projects&project=p&project_view=node-dag&node=alpha&objective=roadmap&pull=7");
assert.equal(projectDashboardUrl("p", "node", { node: "beta", pull: "" }, nodeHome).href,
  "https://horizon.test/?tab=projects&project=p&project_view=node&node=beta&objective=roadmap");
assert.equal(projectDashboardUrl("p", "nodes", {}, nodeHome).href,
  "https://horizon.test/?tab=projects&project=p&project_view=nodes");
assert.equal(projectDashboardUrl("", "overview", {}, nodeHome).href,
  "https://horizon.test/?tab=projects");

console.log("navigation tests passed");
