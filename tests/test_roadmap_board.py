"""Roadmap board metadata: owner, milestone label, and pinned commits."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.roadmap import (
    RoadmapItem,
    item_milestone,
    item_owner,
    item_pinned_commits,
)

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def _item(**meta) -> RoadmapItem:
    return RoadmapItem(id="A", title="t", projects=("ag-main",), metadata=dict(meta))


def test_board_accessors() -> None:
    assert item_owner(_item(owner="team-x")) == "team-x"
    assert item_owner(_item()) == ""
    assert item_milestone(_item(milestone="v1")) == "v1"
    assert item_pinned_commits(_item(pinned_commits=["abc123", "def456"])) == ("abc123", "def456")
    assert item_pinned_commits(_item()) == ()


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    return ws


def test_cli_add_set_and_filter(tmp_path: Path, capsys) -> None:
    ws = _ws(tmp_path)
    main(["--root", str(ws), "roadmap", "add", "--id", "A", "--title", "Core", "--project", "ag-main",
          "--owner", "team-x", "--milestone", "v1"])
    main(["--root", str(ws), "roadmap", "add", "--id", "B", "--title", "Aux", "--project", "ag-main",
          "--milestone", "v2"])
    capsys.readouterr()

    # Pin commits and re-own via `set`.
    main(["--root", str(ws), "roadmap", "set", "A", "--pin-commit", "abc123", "--pin-commit", "def456"])
    capsys.readouterr()
    main(["--root", str(ws), "roadmap", "set", "A", "--unpin-commit", "abc123", "--owner", "team-y", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["owner"] == "team-y"
    assert payload["milestone"] == "v1"
    assert payload["pinned_commits"] == ["def456"]

    # Milestone filter narrows the list.
    main(["--root", str(ws), "roadmap", "list", "--milestone", "v1", "--json"])
    ids = [it["id"] for it in json.loads(capsys.readouterr().out)["items"]]
    assert ids == ["A"]

    # Clearing a field with the empty string.
    main(["--root", str(ws), "roadmap", "set", "A", "--milestone", "", "--json"])
    assert json.loads(capsys.readouterr().out)["milestone"] == ""
