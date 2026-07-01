"""Rendering: structured state -> human-facing artifacts (markdown, HTML)."""

from __future__ import annotations

from .dashboard import render_dashboard

# NB: static_export lives in this package but is intentionally NOT re-exported
# here — it depends on server/config/orchestration, and eagerly importing it
# would create a cycle. Import it as ``archon_horizon.render.static_export``
# directly where needed.
__all__ = ["render_dashboard"]
