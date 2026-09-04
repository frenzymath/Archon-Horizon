"""Static Lean heartbeat benchmark scanner and CLI."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.lean.benchmark import (
    benchmark_project,
    benchmark_workspace,
    parse_heartbeat_value,
    scan_lean_text,
)


SAMPLE = """
import Mathlib

set_option maxHeartbeats 1_000_000

theorem foo : True := by
  set_option maxHeartbeats 400000 in
  trivial

-- set_option maxHeartbeats 999999
set_option synthInstance.maxHeartbeats 200000 in
example : True := trivial

set_option maxRecDepth 1024
"""


def test_parse_heartbeat_value_allows_underscores() -> None:
    assert parse_heartbeat_value("1_000_000") == 1_000_000
    assert parse_heartbeat_value("400000") == 400_000


def test_scan_lean_text_finds_resource_options_not_comments() -> None:
    hits = scan_lean_text(SAMPLE)
    assert [(h.option, h.value) for h in hits] == [
        ("maxHeartbeats", 1_000_000),
        ("maxHeartbeats", 400_000),
        ("synthInstance.maxHeartbeats", 200_000),
        ("maxRecDepth", 1024),
    ]
    assert sum(h.value for h in hits) == 1_000_000 + 400_000 + 200_000 + 1024
    # Commented override must not count.
    assert all("999999" not in h.text for h in hits)


def test_benchmark_project_ranks_by_total(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "A").mkdir(parents=True)
    (root / "A" / "hot.lean").write_text(
        "set_option maxHeartbeats 500000\nset_option maxHeartbeats 100000 in\n",
        "utf-8",
    )
    (root / "cool.lean").write_text("theorem t : True := trivial\n", "utf-8")
    (root / "warm.lean").write_text("set_option synthInstance.maxHeartbeats 50_000\n", "utf-8")
    # Build tree ignored.
    lake = root / ".lake" / "packages" / "mathlib"
    lake.mkdir(parents=True)
    (lake / "ignored.lean").write_text("set_option maxHeartbeats 999999999\n", "utf-8")

    rows = benchmark_project(root, min_heartbeats=1, include_details=False)
    assert [r["path"] for r in rows] == ["A/hot.lean", "warm.lean"]
    assert rows[0]["heartbeats"] == 600_000
    assert rows[0]["hits"] == 2
    assert rows[1]["heartbeats"] == 50_000


def test_benchmark_workspace_tags_project(tmp_path: Path) -> None:
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    p1.mkdir()
    p2.mkdir()
    (p1 / "a.lean").write_text("set_option maxHeartbeats 10\n", "utf-8")
    (p2 / "b.lean").write_text("set_option maxHeartbeats 30\n", "utf-8")
    payload = benchmark_workspace(
        {"p1": p1, "p2": p2},
        min_heartbeats=1,
        include_details=False,
    )
    assert payload["total_files"] == 2
    assert payload["files"][0]["project"] == "p2"
    assert payload["files"][0]["heartbeats"] == 30


def _run(root: Path, *argv: str) -> int:
    return main(["--root", str(root), *argv])


def test_horizon_benchmark_cli_json(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--no-interactive") == 0
    assert _run(ws, "project", "add", "demo", "projects/demo", "--build", "lake build") == 0
    lean = ws / "projects" / "demo" / "Demo.lean"
    lean.parent.mkdir(parents=True, exist_ok=True)
    lean.write_text("set_option maxHeartbeats 12345\ntheorem t : True := trivial\n", "utf-8")
    capsys.readouterr()  # drop init/project chrome before the JSON call

    assert _run(ws, "benchmark", "--json", "-p", "demo") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_files"] == 1
    assert payload["files"][0]["path"] == "Demo.lean"
    assert payload["files"][0]["heartbeats"] == 12345
    assert payload["files"][0]["project"] == "demo"
