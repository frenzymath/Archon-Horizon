"""Wire blueprints to the workspace: project sources -> DAG JSON.

The DAG is built by **hgraph** and nothing else. hgraph resolves ``\\uses`` /
``\\lean`` at sync time against the real Lean sources, so the graph reflects what
is actually proved rather than what the LaTeX claims — and it carries the
per-node layer (chapter, comments, reviews, failure memory) the plain parser
never could. The graph core ships inside Horizon, so there is no external graph
dependency or fallback engine: a project without a blueprint entry simply has
no DAG.

A project must be declared in ``config.yaml`` to get one. There is no
auto-discovery of undeclared directories — the manifest is the source of truth.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from archon_horizon.core.workspace import Workspace
from archon_horizon.log import log

from .checks import is_countable
from .hgraph_graph import build_project_graph as build_hgraph_graph


def _countable_dag(dag: dict) -> dict:
    """Drop prose/proof nodes from a published DAG, including old caches.

    The hgraph adapter applies this filter while rebuilding a project.  A
    dashboard can nevertheless read a JSON snapshot produced by an older
    Horizon process, so apply the same boundary at the cache seam too.  Edges
    to hidden nodes are removed along with the nodes; otherwise a stale remark
    could still affect graph status or dependency counts in the client.
    """
    if not isinstance(dag, dict):
        return dag
    raw_nodes = dag.get("nodes")
    if not isinstance(raw_nodes, list):
        return dag
    nodes = [node for node in raw_nodes if isinstance(node, dict) and is_countable(node)]
    ids = {str(node.get("id")) for node in nodes if node.get("id") is not None}
    out = dict(dag)
    out["nodes"] = nodes
    raw_edges = dag.get("edges")
    if isinstance(raw_edges, list):
        out["edges"] = [
            edge for edge in raw_edges
            if isinstance(edge, dict)
            and str(edge.get("source", "")) in ids
            and str(edge.get("target", "")) in ids
        ]
    return out


def _find_blueprint_dir(
    project_root: Path, configured_path: Path | None, workspace_root: Path | None = None
) -> Path | None:
    if configured_path is not None:
        if configured_path.is_absolute():
            return configured_path if configured_path.exists() else None
        # A configured path may be given relative to the project root or, as the
        # config convention allows, relative to the workspace root. Try both.
        candidates = [project_root / configured_path]
        if workspace_root is not None:
            candidates.append(workspace_root / configured_path)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    # Auto-detect leanblueprint convention or default blueprint folder
    for candidate in ["blueprint/src", "blueprint", "blueprints", "docs/blueprint"]:
        p = project_root / candidate
        if p.is_dir():
            return p

    # Fallback to project root if it contains tex files directly
    if list(project_root.glob("*.tex")):
        return project_root

    return None


def project_dag(workspace: Workspace, name: str, *, sync: bool = True) -> dict | None:
    """The DAG for one project, or None when it has no usable blueprint.

    ``sync=True`` reconciles the blueprint + Lean sources into ``<project>/hgraph/``
    first: authoritative, but it scans the Lean tree and is slow on a large
    blueprint — call it from the build/publish step, never the live server path.
    ``sync=False`` reads the hgraph as it stands, which is the cheap refresh.
    """
    proj = workspace.project(name)
    project_root = workspace.root / proj.path
    bp_dir = _find_blueprint_dir(project_root, proj.blueprint_path, workspace.root)
    return build_hgraph_graph(project_root, bp_dir, sync=sync)


def workspace_dags(
    workspace: Workspace, projects: Sequence[str] | None = None, *, sync: bool = True
) -> dict[str, dict]:
    """DAGs for every configured project that has a usable blueprint.

    ``projects`` restricts the build to a subset — e.g. a run's own scope — so
    other projects' cached DAGs are left untouched. Unknown names are ignored.
    See :func:`project_dag` for ``sync``.

    One project failing to build must not cost every other project its DAG, so a
    failure is reported and skipped rather than raised. It is never silent: a
    project that *should* have a graph and doesn't is exactly the regression this
    used to hide behind a fallback engine.
    """
    out: dict[str, dict] = {}
    for name in _selected_projects(workspace, projects):
        try:
            dag = project_dag(workspace, name, sync=sync)
        except Exception as exc:  # noqa: BLE001 - one bad project, not all of them
            log.warn(f"Blueprint DAG for '{name}' failed to build: {exc}")
            continue
        if dag is not None:
            out[name] = dag
    return out


def published_dags(workspace: Workspace) -> dict[str, dict]:
    """DAGs for the live server: the cached JSON if present, else a fresh build.

    Reading the cache is what keeps the 5s poll cheap; the cache is rewritten
    every round, so the miss path is effectively only a workspace whose DAG has
    never been built. That path must SYNC: an unsynced read of a project with no
    ``<project>/hgraph/`` yet returns an empty graph, which would render as "this
    blueprint has no nodes" rather than as "not built yet". It costs a few
    seconds once, and populates the hgraph for every later read.
    """
    out: dict[str, dict] = {}
    cache_dir = workspace.state_path / "blueprints"
    for name in workspace.projects:
        cached = cache_dir / f"{name}.json"
        if cached.exists():
            try:
                out[name] = _countable_dag(json.loads(cached.read_text("utf-8")))
                continue
            except (OSError, ValueError):
                pass
        dag = project_dag(workspace, name, sync=True)
        if dag is not None:
            out[name] = dag
    return out


def published_dag(workspace: Workspace, name: str) -> dict | None:
    """The published DAG for a single project — the cached JSON if present, else
    a fresh build (see :func:`published_dags` on why the miss path syncs). Same
    source as :func:`published_dags` but for one project, so an on-demand fetch
    of one heavy blueprint doesn't read every project's cache file."""
    if not name:
        return None
    cached = workspace.state_path / "blueprints" / f"{name}.json"
    if cached.exists():
        try:
            return _countable_dag(json.loads(cached.read_text("utf-8")))
        except (OSError, ValueError):
            pass
    try:
        return project_dag(workspace, name, sync=True)
    except KeyError:
        # `name` may be a directory that isn't a configured project (e.g. the
        # static-export output dir) — no blueprint to build.
        return None


def _selected_projects(workspace: Workspace, projects: Sequence[str] | None) -> list[str]:
    """Configured project names, optionally restricted to ``projects``."""
    if projects is None:
        return list(workspace.projects)
    return [name for name in workspace.projects if name in set(projects)]
