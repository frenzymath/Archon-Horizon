"""``horizon leandag`` — inspect the blueprint dependency DAG from the CLI.

The DAG is the leandag engine's output (vendored under ``archon_horizon.leandag``)
serialized to the shared ``{nodes, edges, dangling}`` shape, where an edge
``source -> target`` means *target depends on source*. This command lets an agent
(or a human) verify a project's cones, find isolated/dangling nodes, and view the
union/intersection of several projects' DAGs without opening the dashboard.
"""

from __future__ import annotations

from typing import Any

import re

import typer

from archon_horizon.blueprint.workspace import project_rich_dag
from archon_horizon.search.index import extract_declarations
from archon_horizon.log import log

from .shared import emit_json, load_workspace

Dag = dict[str, Any]


# ── graph helpers (operate on the shared dict shape) ─────────────────────

def _node_ids(dag: Dag) -> set[str]:
    return {n["id"] for n in dag.get("nodes", [])}


def _deps_of(dag: Dag) -> dict[str, set[str]]:
    """node id -> set of ids it directly depends on (edge source for target)."""
    deps: dict[str, set[str]] = {n["id"]: set() for n in dag.get("nodes", [])}
    for e in dag.get("edges", []):
        deps.setdefault(e["target"], set()).add(e["source"])
    return deps


def _rdeps_of(dag: Dag) -> dict[str, set[str]]:
    """node id -> set of ids that directly depend on it."""
    rdeps: dict[str, set[str]] = {n["id"]: set() for n in dag.get("nodes", [])}
    for e in dag.get("edges", []):
        rdeps.setdefault(e["source"], set()).add(e["target"])
    return rdeps


def _cone(dag: Dag, node_id: str) -> set[str]:
    """Transitive dependency cone of ``node_id`` (everything it needs, directly
    or indirectly)."""
    deps = _deps_of(dag)
    seen: set[str] = set()
    frontier = list(deps.get(node_id, set()))
    while frontier:
        cur = frontier.pop()
        if cur in seen:
            continue
        seen.add(cur)
        frontier.extend(deps.get(cur, set()))
    return seen


def _isolated(dag: Dag) -> list[str]:
    """Nodes with no dependency edge in or out."""
    connected: set[str] = set()
    for e in dag.get("edges", []):
        connected.add(e["source"])
        connected.add(e["target"])
    return sorted(n["id"] for n in dag.get("nodes", []) if n["id"] not in connected)


def _proved_count(dag: Dag) -> int:
    return sum(1 for n in dag.get("nodes", []) if n.get("proved") or n.get("leanok"))


def _norm_signature(signature: str) -> str:
    return re.sub(r"\s+", " ", signature.replace("→", "->").replace("⟶", "->")).strip()


def _lean_signature(node: dict[str, Any]) -> str:
    """Best-effort Lean signature for same-id union verification.

    Rich leandag nodes carry Lean source. Parser-fallback nodes may only carry a
    blueprint statement; in that case there is no faithful Lean signature to
    compare, so the duplicate is tolerated.
    """
    source = str(node.get("lean_source") or "")
    if not source.strip():
        return ""
    decls = extract_declarations(source, library="dag", file="node.lean")
    if decls:
        return _norm_signature(" | ".join(d.signature for d in decls))
    head = source.split(":=", 1)[0].split(" where", 1)[0]
    return _norm_signature(head)


def _combine(dags: dict[str, Dag], *, intersect: bool) -> Dag:
    """Union or intersection of several projects' DAGs, keyed by node id.

    Duplicate node ids are intentionally merged. Divergent non-signature fields
    are tolerated because project-local metadata may differ; Lean signature
    divergence is reported as a conflict because it means the shared name no
    longer denotes the same declaration.
    """
    id_sets = [_node_ids(d) for d in dags.values()]
    if intersect:
        keep = set.intersection(*id_sets) if id_sets else set()
    else:
        keep = set.union(*id_sets) if id_sets else set()
    nodes: dict[str, dict] = {}
    signatures: dict[str, dict[str, str]] = {}
    for project, d in dags.items():
        for n in d.get("nodes", []):
            node_id = n["id"]
            if node_id not in keep:
                continue
            sig = _lean_signature(n)
            if sig:
                signatures.setdefault(node_id, {})[project] = sig
            if node_id not in nodes:
                nodes[node_id] = {**n, "project": project}
            else:
                projects = list(nodes[node_id].get("projects", [nodes[node_id].get("project")]))
                projects.append(project)
                nodes[node_id]["projects"] = sorted(p for p in set(projects) if p)
    conflicts = []
    for node_id, by_project in signatures.items():
        if len(set(by_project.values())) > 1:
            conflicts.append({"id": node_id, "signatures": by_project})
    edges = {
        (e["source"], e["target"])
        for d in dags.values()
        for e in d.get("edges", [])
        if e["source"] in keep and e["target"] in keep
    }
    if intersect:
        # keep only edges present in EVERY project (over the shared node set)
        per = [
            {(e["source"], e["target"]) for e in d.get("edges", [])
             if e["source"] in keep and e["target"] in keep}
            for d in dags.values()
        ]
        edges = set.intersection(*per) if per else set()
    return {
        "nodes": list(nodes.values()),
        "edges": [{"source": s, "target": t} for s, t in sorted(edges)],
        "dangling": [],
        "meta": {"signature_conflicts": conflicts},
    }


# ── command ──────────────────────────────────────────────────────────────

class LeanDagCommand:
    def __init__(
        self,
        ctx: typer.Context,
        *,
        projects: list[str] | None,
        node: str | None,
        cone: bool,
        union: bool,
        intersect: bool,
        as_json: bool,
    ) -> None:
        self.root = ctx.obj["root"]
        self.projects = projects or []
        self.node = node
        self.cone = cone
        self.union = union
        self.intersect = intersect
        self.as_json = as_json

    def run(self) -> None:
        _, workspace = load_workspace(self.root)
        names = self.projects or list(workspace.projects)
        dags: dict[str, Dag] = {}
        for name in names:
            dag = project_rich_dag(workspace, name)
            if dag is not None:
                dags[name] = dag
        if not dags:
            if self.as_json:
                emit_json({"projects": []})
            else:
                log.info("no parseable blueprints found")
            raise typer.Exit(0)

        if self.union or self.intersect:
            self._report_combined(dags)
        elif self.node is not None:
            self._report_node(dags)
        else:
            self._report_summary(dags)

    # one combined DAG over several projects
    def _report_combined(self, dags: dict[str, Dag]) -> None:
        combined = _combine(dags, intersect=self.intersect)
        kind = "intersection" if self.intersect else "union"
        conflicts = combined.get("meta", {}).get("signature_conflicts", [])
        if self.as_json:
            emit_json({"mode": kind, "projects": list(dags), "dag": combined})
            if conflicts:
                raise typer.Exit(1)
            return
        log.info(
            f"{kind} of {', '.join(dags)}: {len(combined['nodes'])} nodes, "
            f"{len(combined['edges'])} edges, {len(_isolated(combined))} isolated"
        )
        if conflicts:
            for conflict in conflicts:
                log.warn(f"signature conflict for {conflict['id']}: {', '.join(conflict['signatures'])}")
            raise typer.Exit(1)

    # focus on one node's cone / neighbourhood
    def _report_node(self, dags: dict[str, Dag]) -> None:
        for project, dag in dags.items():
            if self.node not in _node_ids(dag):
                continue
            cone = sorted(_cone(dag, self.node))
            direct = sorted(_deps_of(dag).get(self.node, set()))
            rdeps = sorted(_rdeps_of(dag).get(self.node, set()))
            if self.as_json:
                emit_json({
                    "project": project, "node": self.node,
                    "direct_deps": direct, "cone": cone, "rdeps": rdeps,
                })
                return
            log.info(f"{self.node} in {project}:")
            log.info(f"  direct deps ({len(direct)}): {', '.join(direct) or '—'}")
            log.info(f"  full cone   ({len(cone)}): {', '.join(cone) or '—'}")
            log.info(f"  depended on by ({len(rdeps)}): {', '.join(rdeps) or '—'}")
            return
        msg = f"node {self.node!r} not found in {', '.join(dags)}"
        if self.as_json:
            emit_json({"node": self.node, "error": msg})
        else:
            log.warn(msg)
        raise typer.Exit(1)

    # per-project health summary
    def _report_summary(self, dags: dict[str, Dag]) -> None:
        summary = []
        rows = []
        for project, dag in dags.items():
            isolated = _isolated(dag)
            entry = {
                "project": project,
                "nodes": len(dag.get("nodes", [])),
                "edges": len(dag.get("edges", [])),
                "proved": _proved_count(dag),
                "dangling": len(dag.get("dangling", [])),
                "isolated": isolated,
            }
            summary.append(entry)
            rows.append((
                project,
                f"{entry['nodes']} nodes / {entry['edges']} edges",
                f"{entry['proved']} proved, {len(isolated)} isolated, {entry['dangling']} dangling",
            ))
        if self.as_json:
            emit_json({"projects": summary})
            return
        log.results_table(rows, title="leandag")
        for entry in summary:
            iso = entry["isolated"]
            if iso:
                shown = ", ".join(iso[:15])
                more = f" …(+{len(iso) - 15} more)" if len(iso) > 15 else ""
                log.warn(f"{entry['project']} isolated ({len(iso)}): {shown}{more}")


def leandag(
    ctx: typer.Context,
    projects: list[str] = typer.Option(
        None, "--project", "-p", help="Project to inspect. Repeat for several; default all."
    ),
    node: str = typer.Option(None, "--node", help="Focus on one node (its deps, cone, rdeps)."),
    cone: bool = typer.Option(False, "--cone", help="(with --node) emphasise the full cone."),
    union: bool = typer.Option(False, "--union", help="Combine the selected projects into one DAG."),
    intersect: bool = typer.Option(False, "--intersect", help="Intersect the selected projects' DAGs."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Inspect the blueprint dependency DAG: summaries, a node's cone, or the
    union/intersection of several projects."""
    LeanDagCommand(
        ctx,
        projects=projects,
        node=node,
        cone=cone,
        union=union,
        intersect=intersect,
        as_json=as_json,
    ).run()
