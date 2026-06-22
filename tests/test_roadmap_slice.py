"""The focused slice pulls in direct dependencies across projects."""

from __future__ import annotations

from archon_horizon.core.roadmap import Roadmap, RoadmapItem


def test_slice_includes_direct_dependencies() -> None:
    roadmap = Roadmap(
        items=(
            RoadmapItem(id="R-1", title="main work", projects=("ag-main",), depends_on=("R-2",)),
            RoadmapItem(id="R-2", title="shared lemma", projects=("topology-base",)),
            RoadmapItem(id="R-3", title="unrelated", projects=("other",)),
        )
    )
    sliced = roadmap.slice_for_projects({"ag-main"})
    ids = {i.id for i in sliced.items}
    assert ids == {"R-1", "R-2"}  # R-2 is a cross-project dependency; R-3 excluded
