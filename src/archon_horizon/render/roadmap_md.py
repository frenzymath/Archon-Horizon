"""Render ``roadmap.yaml`` into the human-facing ``reports/roadmap.md``.

``roadmap.yaml`` is the source of truth used for filtering/scheduling;
``roadmap.md`` is a generated artifact. The informal agent owns the roadmap,
so this is regenerated whenever the structured roadmap is saved.
"""

from __future__ import annotations

from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus

_STATUS_ORDER = (
    RoadmapStatus.ACTIVE,
    RoadmapStatus.BLOCKED,
    RoadmapStatus.PENDING,
    RoadmapStatus.DONE,
    RoadmapStatus.REJECTED,
)


def _item_block(item: RoadmapItem) -> str:
    lines = [f"### {item.id} — {item.title}"]
    meta = f"_{item.kind} · priority {item.priority}_"
    if item.projects:
        meta += f" · projects: {', '.join(item.projects)}"
    lines.append(meta)
    if item.summary:
        lines.append("")
        lines.append(item.summary)
    if item.depends_on:
        lines.append("")
        lines.append(f"Depends on: {', '.join(item.depends_on)}")
    return "\n".join(lines)


def render_roadmap_markdown(roadmap: Roadmap) -> str:
    out = ["# Roadmap", "", f"_Generated from roadmap.yaml (v{roadmap.version})._", ""]
    if not roadmap.items:
        out.append("_No roadmap items yet._")
        return "\n".join(out) + "\n"

    by_status: dict[RoadmapStatus, list[RoadmapItem]] = {}
    for item in roadmap.items:
        by_status.setdefault(item.status, []).append(item)

    for status in _STATUS_ORDER:
        items = by_status.get(status)
        if not items:
            continue
        out.append(f"## {status.value.capitalize()}")
        out.append("")
        for item in items:
            out.append(_item_block(item))
            out.append("")
    return "\n".join(out).rstrip() + "\n"
