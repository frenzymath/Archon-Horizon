"""Blueprint -> dependency DAG.

The DAG is built by Horizon's vendored **hgraph** core, which reconciles the
blueprint LaTeX against the real Lean sources. There is no second engine.
"""

from __future__ import annotations

from .hgraph_graph import build_project_graph
from .workspace import project_dag, published_dag, published_dags, workspace_dags

__all__ = [
    "build_project_graph",
    "project_dag",
    "published_dag",
    "published_dags",
    "workspace_dags",
]
