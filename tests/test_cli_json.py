"""--json on the CLI: stdout is pure JSON, chrome/banner go to stderr."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archon_horizon.cli import main

CONFIG = """
workspace:
  name: jsonws
  ground_agent: {harness: g}
  horizon_agent: {harness: h}
harnesses:
  g: {kind: "null"}
  h: {kind: "null"}
projects:
  p: {path: projects/p}
"""


def _workspace(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text(CONFIG, "utf-8")
    (tmp_path / "projects" / "p").mkdir(parents=True)


def _run(tmp_path: Path, *args: str, capsys) -> object:
    code = main(["--root", str(tmp_path), *args])
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(out)  # stdout must be valid JSON and nothing else


def test_inbox_json_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workspace(tmp_path)

    created = _run(tmp_path, "inbox", "add", "--body", "hello world\n\nwith detail", "--json", capsys=capsys)
    item_id = created["id"]
    assert created["body"] == "hello world\n\nwith detail"

    listed = _run(tmp_path, "inbox", "list", "--json", capsys=capsys)
    listed_item = next(i for i in listed["items"] if i["id"] == item_id)
    assert listed_item["scope"] == {}
    assert listed_item["comments"] == []
    assert listed_item["created_at"]

    completed = _run(tmp_path, "inbox", "complete", item_id, "--json", capsys=capsys)
    assert completed == {"id": item_id, "status": "closed"}


def test_inbox_list_filters_and_comment_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workspace(tmp_path)

    first = _run(
        tmp_path,
        "inbox", "add",
        "--body", "Horizon target\n\nfirst body",
        "--to", "horizon",
        "--project", "p",
        "--json",
        capsys=capsys,
    )
    _run(tmp_path, "inbox", "comment", first["id"], "--body", "old comment", "--json", capsys=capsys)
    _run(tmp_path, "inbox", "comment", first["id"], "--body", "new comment", "--json", capsys=capsys)
    _run(
        tmp_path,
        "inbox", "add",
        "--body", "Human notice\n\nsecond body",
        "--to", "human",
        "--json",
        capsys=capsys,
    )

    listed = _run(
        tmp_path,
        "inbox", "list",
        "--to", "horizon",
        "--project", "p",
        "--query", "target",
        "--comments", "1",
        "--json",
        capsys=capsys,
    )

    assert [item["id"] for item in listed["items"]] == [first["id"]]
    assert [comment["body"] for comment in listed["items"][0]["comments"]] == ["new comment"]


def test_blueprint_json_no_blueprints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workspace(tmp_path)
    payload = _run(tmp_path, "blueprint", "--json", capsys=capsys)
    assert payload == {"projects": []}


def test_run_dry_run_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _workspace(tmp_path)
    payload = _run(tmp_path, "run", ".", "--dry-run", "--json", capsys=capsys)
    assert payload["dry_run"] is True
    assert isinstance(payload["rounds"], list)
