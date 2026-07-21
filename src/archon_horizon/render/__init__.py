"""Rendering: structured state -> human-facing artifacts.

The dashboard itself is the React SPA under ``frontend/`` (shipped pre-built
as package data); there is no server-rendered fallback view any more.

NB: static_export lives in this package but is intentionally NOT re-exported
here — it depends on server/config/orchestration, and eagerly importing it
would create a cycle. Import it as ``archon_horizon.render.static_export``
directly where needed.
"""

from __future__ import annotations

__all__: list[str] = []
