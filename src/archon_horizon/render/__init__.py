"""Rendering: structured state -> human-facing artifacts (markdown, HTML)."""

from __future__ import annotations

from .dashboard import render_dashboard
from .roadmap_md import render_roadmap_markdown

# NB: static_export lives in this package but is intentionally NOT re-exported
# here — it depends on server/config/orchestration, and eagerly importing it
# would create a cycle (orchestrator imports render.roadmap_md). Import it as
# ``archon_horizon.render.static_export`` directly where needed.
__all__ = ["render_dashboard", "render_roadmap_markdown"]
