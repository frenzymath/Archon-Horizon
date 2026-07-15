"""Phase 3 (v0.1.1): task↔roadmap dedup. Syncing a task's status to its linked
roadmap milestone propagates the status (and a structured history entry) but must
NOT write a prose comment — that duplicated the task's progress narration onto the
roadmap (the "same comments in both" problem).
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.commands.task import _sync_roadmap_refs_from_task
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.store.filesystem import FilesystemRoadmapStore


def _task(**kw) -> HorizonTask:
    base = dict(
        id="T-1", project="p", objective="do it", title="do it",
        projects=("p",), status=TaskStatus.DONE, write_set=WriteSet(projects=("p",)),
        roadmap_refs=("M-1",),
    )
    base.update(kw)
    return HorizonTask(**base)


def test_status_sync_propagates_status_without_adding_a_comment(tmp_path: Path) -> None:
    rstore = FilesystemRoadmapStore(tmp_path / "roadmap")
    rstore.save(Roadmap(items=(
        RoadmapItem(id="M-1", title="Milestone", projects=("p",), status=RoadmapStatus.ACTIVE),
    )))

    _sync_roadmap_refs_from_task(rstore, _task(), TaskStatus.DONE, "horizon")

    item = {i.id: i for i in rstore.load().items}["M-1"]
    assert item.status is RoadmapStatus.DONE                 # status propagated
    assert not (item.metadata.get("comments") or [])          # but no prose comment
    # The transition is still auditable via structured history, not a comment.
    history = item.metadata.get("history") or []
    assert any(h.get("field") == "status" and h.get("to") == "done" for h in history)


def test_status_sync_is_noop_when_no_roadmap_ref(tmp_path: Path) -> None:
    rstore = FilesystemRoadmapStore(tmp_path / "roadmap")
    rstore.save(Roadmap(items=(
        RoadmapItem(id="M-1", title="Milestone", projects=("p",), status=RoadmapStatus.ACTIVE),
    )))

    _sync_roadmap_refs_from_task(rstore, _task(roadmap_refs=()), TaskStatus.DONE, "horizon")

    item = {i.id: i for i in rstore.load().items}["M-1"]
    assert item.status is RoadmapStatus.ACTIVE  # untouched
