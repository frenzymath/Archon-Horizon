"""Local Lean declaration search ("leansearch").

A LeanSearch-style premise finder that needs **no GPU, no API key, and no
model download**: it extracts declarations from the Lean sources already on
disk and searches them with bm25s (NumPy/SciPy) over names/signatures/docs,
a Loogle-style name match, and a heuristic type-pattern match.
"""

from __future__ import annotations

from .index import Declaration, LeanSearchIndex, SearchHit

__all__ = ["Declaration", "LeanSearchIndex", "SearchHit"]
