export type DashboardTab = "projects" | "references" | "hosts" | "agents" | "search" | "forge" | "zulip" | "activity" | "accounts";
export type ProjectSelection = { node?: string; objective?: string; pull?: string; reference?: string };

/** Normalize shared links from earlier dashboards without overriding explicit views. */
export function dashboardLocation(search: string): URLSearchParams {
  const values = new URLSearchParams(search);
  if (!values.has("view") && values.has("project_view")) values.set("view", values.get("project_view")!);
  values.delete("project_view");
  if (!values.has("tab")) {
    const aliases: Record<string, string> = {work: "activity", changes: "forge", discussions: "zulip", resources: "hosts", settings: "agents"};
    const previous = aliases[values.get("view") || ""];
    values.set("tab", previous || "projects");
    if (previous) values.delete("view");
  }
  if (values.get("tab") === "projects" && values.get("project") && !values.get("view")) {
    const selected = values.get("node") ? "node" : values.get("objective") ? "roadmap"
      : values.get("mission") ? "missions" : values.get("reference") ? "references" : "";
    if (selected) values.set("view", selected);
  }
  if (!values.has("session") && values.has("assignment")) values.set("session", values.get("assignment")!);
  values.delete("assignment");
  return values;
}

const ownedParameters: Record<DashboardTab, readonly string[]> = {
  projects: ["project", "view", "project_view", "node", "objective", "pull", "reference", "run", "mission"],
  agents: ["kind", "name", "session"],
  references: ["project", "reference"],
  search: ["project", "pool", "q", "mode"],
  activity: ["run", "session", "horizon-mention"],
  hosts: [],
  forge: ["project"],
  zulip: ["project"],
  accounts: [],
};

export function dashboardUrl(tab: DashboardTab, current = location.href): URL {
  const url = new URL(current);
  url.search = "";
  url.hash = "";
  url.searchParams.set("tab", tab);
  return url;
}

export function canonicalDashboardUrl(tab: DashboardTab, current = location.href): URL {
  const source = new URL(current);
  const url = dashboardUrl(tab, current);
  for (const name of ownedParameters[tab]) {
    for (const value of source.searchParams.getAll(name)) url.searchParams.append(name, value);
  }
  return url;
}

export function projectDashboardUrl(
  project: string,
  view: string,
  selection: ProjectSelection = {},
  current = location.href,
): URL {
  const source = new URL(current);
  const url = dashboardUrl("projects", current);
  if (project) url.searchParams.set("project", project);
  if (project && view !== "overview") url.searchParams.set("view", view);
  const keepsSelection = project && ["node", "node-dag", "roadmap", "graph"].includes(view);
  for (const key of ["node", "objective", "pull"] as const) {
    const value = selection[key] === undefined ? source.searchParams.get(key) || "" : selection[key];
    const allowed = keepsSelection && (view !== "roadmap" || key === "objective");
    if (allowed && value) url.searchParams.set(key, value);
  }
  const reference = selection.reference === undefined ? source.searchParams.get("reference") || "" : selection.reference;
  if (project && view === "references" && reference) url.searchParams.set("reference", reference);
  return url;
}
