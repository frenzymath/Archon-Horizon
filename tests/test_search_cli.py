"""`horizon search` CLI: --json emits pure JSON on stdout (no banner/chrome)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_horizon.cli import main

CONFIG = """
workspace:
  name: search-cli
  ground_agent: {harness: g}
  horizon_agent: {harness: h}
harnesses:
  g: {kind: "null"}
  h: {kind: "null"}
external_libraries:
  - name: mathlib
    rev: v4.20.0
projects:
  proj: {path: projects/proj}
"""


def _workspace(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(CONFIG, "utf-8")
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    (proj / "Main.lean").write_text(
        "/-- compact image -/\ntheorem isCompact_image : True := trivial\n", "utf-8"
    )


def test_search_json_stdout_is_clean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workspace(tmp_path)
    code = main(["--root", str(tmp_path), "search", "compact image", "--json"])
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out)  # stdout must be valid JSON, nothing else
    assert any(hit["name"] == "isCompact_image" for hit in payload)


def test_search_human_mode_prints_banner_to_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _workspace(tmp_path)
    main(["--root", str(tmp_path), "search", "compact image"])
    captured = capsys.readouterr()

    # Banner + table are chrome on stderr; stdout has no banner.
    assert "Archon Horizon" not in captured.out
