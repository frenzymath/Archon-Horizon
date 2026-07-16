"""Wire blueprints to the workspace: read project sources -> parsed DAG.

The roadmap wants the DAG generated directly from parsed blueprint source (no
``leanblueprint web`` build step). A project's ``blueprint.path`` directory is
globbed for ``.tex``, concatenated, parsed once, and turned into the DAG JSON
the dashboard and the DAG-consistency subagent consume.
"""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.core.workspace import Workspace

from .dag import build_dag
from .hgraph_graph import build_project_graph as build_hgraph_graph
from .leandag_graph import build_project_graph
from .model import Blueprint
from .parser import parse_blueprint


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

def project_blueprint(workspace: Workspace, name: str) -> Blueprint | None:
    """Parse one project's blueprint sources into a :class:`Blueprint`, or None."""
    proj = workspace.project(name)
    bp_dir = _find_blueprint_dir(workspace.root / proj.path, proj.blueprint_path, workspace.root)
    if not bp_dir:
        return None
    sources = sorted(bp_dir.rglob("*.tex"))
    if not sources:
        return None
    text = "\n".join(path.read_text("utf-8") for path in sources)
    return parse_blueprint(text)


def project_dag(workspace: Workspace, name: str) -> dict | None:
    """Build the dependency DAG for one project's blueprint, or None."""
    blueprint = project_blueprint(workspace, name)
    return build_dag(blueprint) if blueprint is not None else None


def project_rich_dag(workspace: Workspace, name: str) -> dict | None:
    """Rich graph for one project: hgraph → leandag → parser DAG.

    hgraph is preferred when installed — its sync also maintains the per-node
    files (with agent comments/reviews) under ``<project>/hgraph/``. Slow (both
    engines scan the Lean tree) — call from the build/cache step, not the live
    per-poll server path.
    """
    proj = workspace.project(name)
    project_root = workspace.root / proj.path
    bp_dir = _find_blueprint_dir(project_root, proj.blueprint_path, workspace.root)
    rich = build_hgraph_graph(project_root, bp_dir)
    if rich is None:
        rich = build_project_graph(project_root, bp_dir)
    if rich is not None:
        return rich
    return project_dag(workspace, name)


def workspace_dags_rich(workspace: Workspace) -> dict[str, dict]:
    """Rich DAGs for every project (hgraph/leandag when available, else parser)."""
    out: dict[str, dict] = {}
    for name in workspace.projects:
        dag = project_rich_dag(workspace, name)
        if dag is not None:
            out[name] = dag
    return out or workspace_dags(workspace)


def published_dags(workspace: Workspace) -> dict[str, dict]:
    """DAGs for the live server: the cached (possibly rich) JSON if present,
    else a fresh parser DAG. Reading the cache keeps the 5s poll cheap even when
    the cache was built with leandag."""
    out: dict[str, dict] = {}
    cache_dir = workspace.state_path / "blueprints"
    for name in workspace.projects:
        cached = cache_dir / f"{name}.json"
        if cached.exists():
            try:
                out[name] = json.loads(cached.read_text("utf-8"))
                continue
            except (OSError, ValueError):
                pass
        dag = project_dag(workspace, name)
        if dag is not None:
            out[name] = dag
    return out


def published_dag(workspace: Workspace, name: str) -> dict | None:
    """The published (possibly rich) DAG for a single project — the cached JSON
    if present, else a fresh parser DAG. Same source as :func:`published_dags`
    but for one project, so an on-demand fetch of one heavy blueprint doesn't
    read every project's cache file."""
    if not name:
        return None
    cached = workspace.state_path / "blueprints" / f"{name}.json"
    if cached.exists():
        try:
            return json.loads(cached.read_text("utf-8"))
        except (OSError, ValueError):
            pass
    try:
        return project_dag(workspace, name)
    except KeyError:
        # `name` may be an auto-discovered directory that isn't a configured
        # project (e.g. the static-export output dir) — no blueprint to build.
        return None


def workspace_dags(workspace: Workspace) -> dict[str, dict]:
    """DAGs for every project that has a parseable blueprint."""
    out: dict[str, dict] = {}
    
    # Configured projects
    for name in workspace.projects:
        dag = project_dag(workspace, name)
        if dag is not None:
            out[name] = dag
            
    # Auto-detect unconfigured projects in the workspace root
    if not out:
        for p in workspace.root.iterdir():
            if p.is_dir() and not p.name.startswith(".") and p.name != "src":
                bp_dir = _find_blueprint_dir(p, None)
                if bp_dir:
                    sources = sorted(bp_dir.rglob("*.tex"))
                    if sources:
                        text = "\n".join(path.read_text("utf-8") for path in sources)
                        blueprint = parse_blueprint(text)
                        if blueprint:
                            dag = build_dag(blueprint)
                            if dag:
                                out[p.name] = dag
                                
        # Also try workspace root itself as a single project
        bp_dir = _find_blueprint_dir(workspace.root, None)
        if bp_dir and workspace.name not in out:
            sources = sorted(bp_dir.rglob("*.tex"))
            if sources:
                text = "\n".join(path.read_text("utf-8") for path in sources)
                blueprint = parse_blueprint(text)
                if blueprint:
                    dag = build_dag(blueprint)
                    if dag:
                        out[workspace.name] = dag
                        
    return out
