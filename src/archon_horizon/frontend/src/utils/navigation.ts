export type DashboardTab = "projects" | "hosts" | "agents" | "search" | "forge" | "zulip" | "activity" | "accounts";
export type ProjectSelection = { node?: string; objective?: string; pull?: string };

const ownedParameters: Record<DashboardTab, readonly string[]> = {
  projects: ["project", "project_view", "node", "objective", "pull", "run", "mission"],
  agents: ["kind", "name", "session"],
  search: ["project", "pool", "q", "mode"],
  activity: ["run", "session", "horizon-mention"],
  hosts: [],
  forge: [],
  zulip: [],
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
  if (project && view !== "overview") url.searchParams.set("project_view", view);
  const keepsSelection = project && ["node", "node-dag", "roadmap", "graph"].includes(view);
  for (const key of ["node", "objective", "pull"] as const) {
    const value = selection[key] === undefined ? source.searchParams.get(key) || "" : selection[key];
    const allowed = keepsSelection && (view !== "roadmap" || key === "objective");
    if (allowed && value) url.searchParams.set(key, value);
  }
  return url;
}
