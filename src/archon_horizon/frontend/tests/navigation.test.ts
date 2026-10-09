import assert from "node:assert/strict";
import { canonicalDashboardUrl, dashboardLocation, dashboardUrl, projectDashboardUrl } from "../src/utils/navigation";

assert.equal(dashboardLocation("?project=p&node=n").get("view"), "node");
assert.equal(dashboardLocation("?project=p&objective=o").get("view"), "roadmap");
assert.equal(dashboardLocation("?project=p&mission=m").get("view"), "missions");
assert.equal(dashboardLocation("?project=p&reference=r").get("view"), "references");
assert.equal(dashboardLocation("?project=p&view=overview&node=n").get("view"), "overview");
assert.equal(dashboardLocation("?project=p&project_view=node-dag&node=n").get("view"), "node-dag");
assert.equal(dashboardLocation("?tab=activity&project=p&node=n").get("view"), null);
assert.equal(dashboardLocation("?view=work&assignment=a").get("session"), "a");
assert.equal(dashboardLocation("?view=work&assignment=a").get("tab"), "activity");

const dirty = "https://horizon.test/?tab=zulip&kind=hooks&name=preflight&project=p&node=n&proposal=legacy&run=r#fragment";

assert.equal(dashboardUrl("zulip", dirty).href, "https://horizon.test/?tab=zulip");
assert.equal(dashboardUrl("search", dirty).href, "https://horizon.test/?tab=search");
assert.equal(dashboardUrl("accounts", dirty).href, "https://horizon.test/?tab=accounts");
assert.equal(dashboardUrl("projects", dirty).href, "https://horizon.test/?tab=projects");
assert.equal(canonicalDashboardUrl("zulip", dirty).href, "https://horizon.test/?tab=zulip&project=p");
assert.equal(canonicalDashboardUrl("references", "https://horizon.test/?project=p&reference=ref&node=n").href,
  "https://horizon.test/?tab=references&project=p&reference=ref");
assert.equal(canonicalDashboardUrl("agents", dirty).href, "https://horizon.test/?tab=agents&kind=hooks&name=preflight");
assert.equal(canonicalDashboardUrl("activity", dirty).href, "https://horizon.test/?tab=activity&run=r");
assert.equal(canonicalDashboardUrl("projects", dirty).href, "https://horizon.test/?tab=projects&project=p&node=n&run=r");

const nodeHome = "https://horizon.test/?tab=projects&project=p&project_view=node&node=alpha&objective=roadmap&pull=7";
assert.equal(projectDashboardUrl("p", "node-dag", {}, nodeHome).href,
  "https://horizon.test/?tab=projects&project=p&view=node-dag&node=alpha&objective=roadmap&pull=7");
assert.equal(projectDashboardUrl("p", "node", { node: "beta", pull: "" }, nodeHome).href,
  "https://horizon.test/?tab=projects&project=p&view=node&node=beta&objective=roadmap");
assert.equal(projectDashboardUrl("p", "nodes", {}, nodeHome).href,
  "https://horizon.test/?tab=projects&project=p&view=nodes");
assert.equal(projectDashboardUrl("", "overview", {}, nodeHome).href,
  "https://horizon.test/?tab=projects");

console.log("navigation tests passed");
