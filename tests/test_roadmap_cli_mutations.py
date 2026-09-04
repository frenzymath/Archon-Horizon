"""Roadmap CLI: show, depends-on, rename, move, remove cascade."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main

_CONFIG = """
workspace:
  name: w
  rounds: 1
  horizon_agent: {harness: hor}
harnesses:
  hor: {kind: "null"}
projects:
  p: {path: projects/p}
"""


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "projects" / "p").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    return ws


def test_show_set_depends_rename_remove(tmp_path: Path, capsys) -> None:
    ws = _ws(tmp_path)
    assert main(["--root", str(ws), "roadmap", "add", "--id", "A", "--title", "goal",
                 "--project", "p", "--milestone", "phase-1"]) == 0
    assert main(["--root", str(ws), "roadmap", "add", "--id", "A.1", "--title", "step",
                 "--project", "p", "--parent", "A", "--depends-on", "A"]) == 0
    assert main(["--root", str(ws), "roadmap", "add", "--id", "B", "--title", "side",
                 "--project", "p"]) == 0
    capsys.readouterr()

    assert main(["--root", str(ws), "roadmap", "show", "A.1", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["id"] == "A.1"
    assert shown["parent"] == "A"
    assert shown["depends_on"] == ["A"]
    assert shown["milestone"] == ""

    # Move under B, replace depends-on, set projects/refs.
    assert main([
        "--root", str(ws), "roadmap", "set", "A.1",
        "--parent", "B",
        "--depends-on", "B",
        "--project", "p",
        "--task-ref", "T-1",
        "--json",
    ]) == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["parent"] == "B"
    assert moved["depends_on"] == ["B"]
    assert moved["task_refs"] == ["T-1"]
    assert moved["projects"] == ["p"]

    # Clear depends-on with empty string.
    assert main(["--root", str(ws), "roadmap", "set", "A.1", "--depends-on", "", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["depends_on"] == []

    # Rename rewrites parent links pointing at the old id.
    assert main(["--root", str(ws), "roadmap", "rename", "B", "C", "--json"]) == 0
    renamed = json.loads(capsys.readouterr().out)
    assert renamed["id"] == "C"
    assert renamed["renamed_from"] == "B"
    assert main(["--root", str(ws), "roadmap", "show", "A.1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["parent"] == "C"

    # Default remove un-nests children; cascade deletes them.
    assert main(["--root", str(ws), "roadmap", "remove", "C", "--json"]) == 0
    removed = json.loads(capsys.readouterr().out)
    assert removed["removed"] == ["C"]
    assert main(["--root", str(ws), "roadmap", "show", "A.1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["parent"] == ""

    assert main(["--root", str(ws), "roadmap", "set", "A.1", "--parent", "A"]) == 0
    capsys.readouterr()
    assert main(["--root", str(ws), "roadmap", "remove", "A", "--cascade", "--json"]) == 0
    cascaded = json.loads(capsys.readouterr().out)
    assert set(cascaded["removed"]) == {"A", "A.1"}
    assert main(["--root", str(ws), "roadmap", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == []
