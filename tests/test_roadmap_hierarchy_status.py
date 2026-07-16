"""Roadmap parent↔child status consistency: warned, never auto-corrected."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.roadmap import (
    RoadmapItem,
    RoadmapStatus,
    hierarchy_status_warnings,
    subtree_progress,
)


def _item(item_id: str, status: str, parent: str | None = None) -> RoadmapItem:
    meta = {"parent": parent} if parent else {}
    return RoadmapItem(
        id=item_id, title=item_id, projects=("p",), status=RoadmapStatus(status), metadata=meta
    )


def test_all_children_done_suggests_closing_the_parent() -> None:
    items = (_item("A", "active"), _item("A.1", "done", "A"), _item("A.2", "done", "A"))
    warnings = hierarchy_status_warnings(items)
    assert len(warnings) == 1
    assert "A" in warnings[0] and "--status done" in warnings[0]


def test_done_parent_with_open_child_is_flagged() -> None:
    items = (_item("A", "done"), _item("A.1", "done", "A"), _item("A.2", "active", "A"))
    warnings = hierarchy_status_warnings(items)
    assert len(warnings) == 1
    assert "A is done" in warnings[0] and "A.2" in warnings[0]


def test_consistent_trees_and_rejected_children_are_quiet() -> None:
    # In-progress tree: no warning.
    assert hierarchy_status_warnings((_item("A", "active"), _item("A.1", "active", "A"))) == []
    # Fully done tree: no warning.
    assert hierarchy_status_warnings((_item("A", "done"), _item("A.1", "done", "A"))) == []
    # A rejected child neither blocks closing nor counts as open.
    items = (_item("A", "done"), _item("A.1", "done", "A"), _item("A.2", "rejected", "A"))
    assert hierarchy_status_warnings(items) == []


def test_subtree_progress_counts_descendants() -> None:
    items = (
        _item("A", "active"),
        _item("A.1", "done", "A"),
        _item("A.2", "active", "A"),
        _item("A.2.1", "done", "A.2"),
        _item("A.3", "rejected", "A"),  # rejected: excluded from both counts
    )
    progress = subtree_progress(items)
    assert progress["A"] == (2, 3)     # A.1, A.2, A.2.1 counted; A.3 excluded
    assert progress["A.2"] == (1, 1)


_CONFIG = "workspace:\n  name: w\n  horizon_agent: {harness: hor}\nharnesses:\n  hor: {kind: \"null\"}\nprojects:\n  p: {path: projects/p}\n"


def test_cli_surfaces_warnings_and_progress(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "p").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")

    assert main(["--root", str(ws), "roadmap", "add", "--id", "A", "--title", "goal", "--project", "p"]) == 0
    assert main(["--root", str(ws), "roadmap", "add", "--id", "A.1", "--title", "step", "--project", "p", "--parent", "A"]) == 0
    capsys.readouterr()

    # Closing the child makes the subtree complete: `set` reports the suggestion.
    assert main(["--root", str(ws), "roadmap", "set", "A.1", "--status", "done", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("--status done" in w for w in payload["warnings"])

    # The tree view carries the parent's subtree progress + the warning.
    assert main(["--root", str(ws), "roadmap", "list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    parent_row = next(r for r in listing["items"] if r["id"] == "A")
    assert (parent_row["subtree_done"], parent_row["subtree_total"]) == (1, 1)
    assert any("A" in w for w in listing["warnings"])

    # Nothing was auto-corrected: A keeps its original (pending) status.
    assert parent_row["status"] == "pending"
