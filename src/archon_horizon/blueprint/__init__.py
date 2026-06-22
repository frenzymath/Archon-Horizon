"""Blueprint parsing and DAG generation.

One parser (ported from Archon's KaTeX renderer) feeds both the dependency
DAG and, later, HTML rendering — no plasTeX/leanblueprint dependency.
"""

from __future__ import annotations

from .dag import build_dag
from .model import Blueprint, BlueprintNode
from .parser import KNOWN_ENVS, parse_blueprint, strip_comments
from .workspace import project_blueprint, project_dag, workspace_dags

__all__ = [
    "KNOWN_ENVS",
    "Blueprint",
    "BlueprintNode",
    "build_dag",
    "parse_blueprint",
    "project_blueprint",
    "project_dag",
    "strip_comments",
    "workspace_dags",
]
