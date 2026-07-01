"""Renderers: the static dashboard HTML."""

from __future__ import annotations

from archon_horizon.core.inbox import InboxItem, InboxKind
from archon_horizon.core.labels import AGENT_READY
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.render.dashboard import render_dashboard


def _roadmap() -> Roadmap:
    return Roadmap(
        items=(
            RoadmapItem(id="R-1", title="Active thing", projects=("a",), status=RoadmapStatus.ACTIVE),
            RoadmapItem(id="R-2", title="Done thing", projects=("a",), status=RoadmapStatus.DONE),
        )
    )


def test_dashboard_html_is_self_contained_and_escapes() -> None:
    item = InboxItem(
        id="I-1", provider="local", kind=InboxKind.HINT,
        body="<script>evil</script>", labels=(AGENT_READY,),
    )
    html = render_dashboard(
        workspace_name="demo",
        roadmap=_roadmap(),
        local_items=[item],
        memory="be careful",
        reports=["roadmap"],
        dag={"nodes": [{"id": "n1", "kind": "lemma", "leanok": True}], "edges": []},
    )
    assert html.startswith("<!doctype html>")
    assert "&lt;script&gt;" in html  # body is escaped, not injected
    assert "<script>evil</script>" not in html
    assert "1 nodes, 0 edges" in html
