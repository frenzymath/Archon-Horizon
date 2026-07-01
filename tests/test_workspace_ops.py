"""Structural project operations mutate config.yaml and move files."""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.config import operations
from archon_horizon.config.loader import load_config


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    return ws


def test_add_and_remove(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    operations.add_project(ws, "a", "projects/a")
    assert "a" in load_config(ws).projects
    with pytest.raises(ValueError):
        operations.add_project(ws, "a", "projects/a")  # duplicate
    operations.remove_project(ws, "a")
    assert "a" not in load_config(ws).projects


def test_archive_moves_tree(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    operations.add_project(ws, "a", "projects/a")
    (ws / "projects" / "a" / "Foo.lean").write_text("def foo := 1\n", "utf-8")
    dest = operations.archive_project(ws, "a")
    assert "a" not in load_config(ws).projects
    assert (dest / "Foo.lean").exists()
    assert not (ws / "projects" / "a").exists()


def test_merge_moves_files_and_drops_source(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    operations.add_project(ws, "dst", "projects/dst")
    operations.add_project(ws, "src", "projects/src")
    (ws / "projects" / "src" / "Bar.lean").write_text("def bar := 2\n", "utf-8")
    operations.merge_projects(ws, "dst", "src")
    assert "src" not in load_config(ws).projects
    assert (ws / "projects" / "dst" / "Bar.lean").exists()
