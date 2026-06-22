"""Wire blueprints to the workspace: read project sources -> parsed DAG.

The roadmap wants the DAG generated directly from parsed blueprint source (no
``leanblueprint web`` build step). A project's ``blueprint.path`` directory is
globbed for ``.tex``, concatenated, parsed once, and turned into the DAG JSON
the dashboard and the DAG-consistency subagent consume.
"""

from __future__ import annotations

from archon_horizon.core.workspace import Workspace

from .dag import build_dag
from .model import Blueprint
from .parser import parse_blueprint


def project_blueprint(workspace: Workspace, name: str) -> Blueprint | None:
    """Parse one project's blueprint sources into a :class:`Blueprint`, or None."""
    blueprint_path = workspace.project(name).blueprint_path
    if blueprint_path is None:
        return None
    bp_dir = blueprint_path if blueprint_path.is_absolute() else workspace.root / blueprint_path
    if not bp_dir.exists():
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


def workspace_dags(workspace: Workspace) -> dict[str, dict]:
    """DAGs for every project that has a parseable blueprint."""
    out: dict[str, dict] = {}
    for name in workspace.projects:
        dag = project_dag(workspace, name)
        if dag is not None:
            out[name] = dag
    return out
