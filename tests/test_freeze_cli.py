"""The freeze CLI maintains config-backed rules enforced before dispatch."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from archon_horizon.cli import main
from archon_horizon.config.loader import build_freeze, load_config
from archon_horizon.core.freeze import freeze_violation_details, frozen_violations
from archon_horizon.core.tasks import WriteSet


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "config.yaml").write_text(
        "workspace: {name: test}\nprojects:\n  demo: {path: projects/demo}\n", "utf-8"
    )
    return root


def test_freeze_add_list_remove_roundtrip(tmp_path: Path, capsys) -> None:
    root = _workspace(tmp_path)
    assert main(["--root", str(root), "freeze", "add", "file", "Demo/API.lean"]) == 0
    assert main(["--root", str(root), "freeze", "add", "declaration", "Demo.api"]) == 0
    assert main(["--root", str(root), "freeze", "add", "blueprint-node", "thm:api"]) == 0

    raw = yaml.safe_load((root / "config.yaml").read_text("utf-8"))
    assert raw["freeze"] == {
        "files": ["Demo/API.lean"],
        "declarations": ["Demo.api"],
        "blueprint_nodes": ["thm:api"],
    }
    freeze = build_freeze(load_config(root))
    violations = frozen_violations(
        WriteSet(files=("Demo/API.lean",), declarations=("Demo.api",), blueprint_nodes=("thm:api",)),
        freeze,
    )
    assert {rule.pattern for rule in violations} == {"Demo/API.lean", "Demo.api", "thm:api"}
    details = freeze_violation_details(
        WriteSet(files=("Demo/API.lean",), declarations=("Demo.api",)), freeze
    )
    assert {(detail.target, detail.rule.metadata["config_key"]) for detail in details} == {
        ("Demo/API.lean", "freeze.files"),
        ("Demo.api", "freeze.declarations"),
    }

    capsys.readouterr()
    assert main(["--root", str(root), "freeze", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rules"]["blueprint-node"] == ["thm:api"]

    assert main(["--root", str(root), "freeze", "list"]) == 0

    assert main(["--root", str(root), "freeze", "remove", "blueprint_node", "thm:api"]) == 0
    assert "blueprint_nodes" not in yaml.safe_load((root / "config.yaml").read_text("utf-8"))["freeze"]


def test_freeze_add_is_idempotent(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    args = ["--root", str(root), "freeze", "add", "file", "Demo/*.lean"]
    assert main(args) == 0
    assert main(args) == 0
    raw = yaml.safe_load((root / "config.yaml").read_text("utf-8"))
    assert raw["freeze"]["files"] == ["Demo/*.lean"]


def test_freeze_rejects_unknown_kind_without_traceback(tmp_path: Path) -> None:
    root = _workspace(tmp_path)
    assert main(["--root", str(root), "freeze", "add", "unknown", "x"]) == 2
