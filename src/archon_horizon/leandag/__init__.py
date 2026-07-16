"""leandag — dependency graph and complexity metrics for Lean 4 + leanblueprint projects.

Vendored into Archon Horizon from https://github.com/AxelDlv00/LeanDAG (v0.1.0)
so the dependency-graph engine ships in-repo (no external git install, and we can
extend it — e.g. cross-project union/intersection). Only the slice Horizon
consumes is kept: scanner + blueprint parser + DAG build (see
``blueprint/leandag_graph.py``); upstream's exporters/queries/reporter are not
shipped.
"""

__version__ = "0.1.0"

from .dag import DAG
from .models import BlueprintDecl, Edge, GraphNode, LeanDecl
from .parser import BlueprintParser
from .scanner import LeanScanner

__all__ = [
    "BlueprintDecl", "LeanDecl", "GraphNode", "Edge",
    "DAG", "BlueprintParser", "LeanScanner",
    "__version__",
]
