export type CapabilityReference = { kind: string; name: string };
const kinds = new Set(["skills", "agents", "mcp", "plugins", "hooks"]);

export function capabilityHref(kind: string, name: string, session?: string): string {
  const params = new URLSearchParams({ tab: "agents", kind, name });
  if (session) params.set("session", session);
  return `/?${params}`;
}

export function capabilityReference(kind: string, data: Record<string, unknown>): CapabilityReference | undefined {
  const supplied = data.capability as Partial<CapabilityReference> | undefined;
  let category: unknown, name: unknown;
  if (supplied && typeof supplied === "object") { category = supplied.kind; name = supplied.name; }
  else if (kind.startsWith("skill.")) { category = "skills"; name = data.skill || data.name; }
  else if (kind.startsWith("mcp.")) { category = "mcp"; name = data.server || data.name; }
  else if (kind.startsWith("plugin.")) { category = "plugins"; name = data.plugin || data.name; }
  else if (kind.startsWith("hook.")) { category = "hooks"; name = data.hook || data.name; }
  else if (kind === "report.stop_gate") { category = "hooks"; name = "Report stop gate"; }
  else if (kind.startsWith("session.")) { category = "agents"; name = data.agent; }
  if (typeof category === "string" && kinds.has(category) && typeof name === "string" && name.length > 0 && name.length <= 200) return { kind: category, name };
}
