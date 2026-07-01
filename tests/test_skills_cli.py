"""`horizon skills install` refreshes a workspace's stale .claude/skills/."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main


def test_skills_install_refreshes_stale_workspace_skill(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0

    skill = ws / ".claude" / "skills" / "horizon-inbox" / "SKILL.md"
    assert skill.exists()
    skill.write_text("STALE — old workspace copy", "utf-8")  # simulate a pre-upgrade copy

    assert main(["--root", str(ws), "skills", "install"]) == 0
    refreshed = skill.read_text("utf-8")
    assert "STALE" not in refreshed
    assert "Markdown" in refreshed  # the bundled skill teaches the Markdown convention

    # A second install is a no-op (nothing differs from the bundled version).
    assert main(["--root", str(ws), "skills", "install"]) == 0


def test_skills_list_runs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0
    assert main(["--root", str(ws), "skills", "list"]) == 0


def test_init_update_refreshes_skills_and_keeps_config(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0
    config = (ws / "config.yaml").read_text("utf-8")
    skill = ws / ".claude" / "skills" / "horizon-inbox" / "SKILL.md"
    skill.write_text("STALE — pre-upgrade copy", "utf-8")

    # A plain non-interactive reinit refuses (won't clobber an existing workspace).
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 1

    # `--update` is the reinit/refresh path: it updates derived artifacts and
    # keeps the config + content.
    assert main(["--root", str(ws), "init", "--update"]) == 0
    refreshed = skill.read_text("utf-8")
    assert "STALE" not in refreshed and "Markdown" in refreshed
    assert (ws / "config.yaml").read_text("utf-8") == config
