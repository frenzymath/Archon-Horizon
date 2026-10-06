type Mission = { id: string; parent_id?: string | null; created_at?: string };

export function missionTree<T extends Mission>(missions: T[], matches: (mission: T) => boolean, collapsed: Set<string>) {
  const ordered = missions.map((mission) => ({ mission, created: Date.parse(mission.created_at ?? "") }))
    .sort((a, b) => (Number.isFinite(b.created) ? b.created : -Infinity) - (Number.isFinite(a.created) ? a.created : -Infinity))
    .map(({ mission }) => mission);
  const byId = new Map(missions.map((mission) => [mission.id, mission]));
  const matching = new Set(missions.filter(matches).map((mission) => mission.id));
  const included = new Set<string>();
  for (const id of matching) {
    let cursor = byId.get(id);
    while (cursor && !included.has(cursor.id)) {
      included.add(cursor.id);
      cursor = byId.get(cursor.parent_id ?? "");
    }
  }
  const children = new Map<string, T[]>();
  const roots: T[] = [];
  for (const mission of ordered) {
    if (!included.has(mission.id)) continue;
    if (!mission.parent_id || !byId.has(mission.parent_id)) roots.push(mission);
    else {
      const siblings = children.get(mission.parent_id) ?? [];
      siblings.push(mission);
      children.set(mission.parent_id, siblings);
    }
  }
  const rows: { mission: T; depth: number; childCount: number; matches: boolean }[] = [];
  const visited = new Set<string>();
  const visit = (root: T) => {
    const stack = [{ mission: root, depth: 0, hidden: false }];
    while (stack.length) {
      const { mission, depth, hidden } = stack.pop()!;
      if (visited.has(mission.id)) continue;
      visited.add(mission.id);
      const descendants = children.get(mission.id) ?? [];
      if (!hidden) rows.push({ mission, depth, childCount: descendants.length, matches: matching.has(mission.id) });
      for (let index = descendants.length - 1; index >= 0; index -= 1) {
        stack.push({ mission: descendants[index], depth: depth + 1, hidden: hidden || collapsed.has(mission.id) });
      }
    }
  };
  roots.forEach(visit);
  // Keep incomplete or cyclic snapshots inspectable without recursion or duplicates.
  ordered.forEach((mission) => { if (included.has(mission.id) && !visited.has(mission.id)) visit(mission); });
  return { rows, matchingCount: matching.size, branches: [...children.keys()] };
}
