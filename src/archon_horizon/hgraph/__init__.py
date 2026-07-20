"""Horizon's vendored hgraph core — a plain-files semantic graph.

Nodes and edges are Markdown/YAML files under ``<project>/hgraph/``; drive them
with ``horizon graph`` or this API. See ``graph.py``.
"""

from .graph import Edge, Graph, HGraphError, Node

__all__ = ["Graph", "Node", "Edge", "HGraphError"]
