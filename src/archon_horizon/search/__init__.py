"""Local, dependency-free Lean declaration search ("leansearch").

A LeanSearch-style premise finder that needs **no GPU, no API key, and no
model download**: it extracts declarations from the Lean sources already on
disk (the workspace projects plus the libraries listed under
``external_libraries``) and searches them three ways — a pure-Python BM25 index
over names/signatures/docstrings for natural-language queries, a Loogle-style
name match, and a heuristic type-pattern match.
"""

from __future__ import annotations

from .index import Declaration, LeanSearchIndex, SearchHit

__all__ = ["Declaration", "LeanSearchIndex", "SearchHit"]
