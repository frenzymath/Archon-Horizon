"""CLI surfacing for advisory collection-health warnings."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main


_CONFIG = """\
workspace:
  name: health-test
  horizon_agent: {harness: hor}
harnesses:
  hor: {kind: "null"}
projects:
  p: {path: projects/p}
"""


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "projects" / "p").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    return root


def test_cli_lists_surface_health_warnings_in_human_and_json_output(
    tmp_path: Path, capsys
) -> None:
    root = _workspace(tmp_path)

    for index in range(11):
        assert main([
            "--root", str(root), "inbox", "add", "--kind", "memory",
            "--body", f"Memory {index}\n\nDurable detail.",
        ]) == 0
    for index in range(13):
        assert main([
            "--root", str(root), "task", "add", "--id", f"T-{index}",
            "--project", "p", "--objective", "test",
        ]) == 0
    for index in range(9):
        assert main([
            "--root", str(root), "roadmap", "add", "--id", f"R-{index}",
            "--title", "test", "--project", "p", "--status", "active",
        ]) == 0
    capsys.readouterr()

    assert main(["--root", str(root), "inbox", "list"]) == 0
    human = capsys.readouterr()
    assert "11 open memory items" in human.out + human.err

    for command, expected in (
        (("inbox", "list"), "11 open memory items"),
        (("task", "list"), "13 open tasks"),
        (("roadmap", "list"), "9 active items"),
    ):
        assert main(["--root", str(root), *command, "--json"]) == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert any(expected in warning for warning in payload["warnings"])


def test_agent_inbox_health_counts_only_items_the_session_can_list(
    tmp_path: Path, capsys, monkeypatch,
) -> None:
    root = _workspace(tmp_path)
    for index in range(10):
        main([
            "--root", str(root), "inbox", "add", "--kind", "memory",
            "--body", f"Visible memory {index}\n\nDurable detail.",
        ])
    main([
        "--root", str(root), "inbox", "add", "--kind", "memory", "--to", "human",
        "--body", "Human-only memory\n\nThis is outside the agent's actionable queue.",
    ])
    capsys.readouterr()

    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "0001-horizon-T-1")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "p")

    assert main(["--root", str(root), "inbox", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["items"]) == 10
    assert payload["attention"]["advisory_unread_count"] == 10
    assert not any("open memory items" in warning for warning in payload["warnings"])
