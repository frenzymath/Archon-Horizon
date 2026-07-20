"""A workspace's skills drift from the bundled ones, and nothing said so.

`install_skills` runs only on `init` / `horizon skills install`, so a long-lived
workspace keeps whatever guidance it was initialized with while the package moves
on. Agents then follow text that no longer matches their tools — silently. The
run surfaces the drift instead of rewriting it, because the `horizon` skill is
advertised as per-workspace editable and a difference may be deliberate.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.skills.registry import install_skills, stale_skills


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    return ws


def test_freshly_installed_workspace_has_no_drift(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    install_skills(ws)
    assert stale_skills(ws) == []


def test_edited_skill_is_reported_stale(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    install_skills(ws)
    (ws / ".claude/skills/horizon/SKILL.md").write_text("locally edited\n", "utf-8")
    assert "horizon" in stale_skills(ws)


def test_missing_skill_is_reported_stale(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    install_skills(ws)
    target = ws / ".claude/skills/leansearch/SKILL.md"
    target.unlink()
    assert "leansearch" in stale_skills(ws)


def test_drift_detection_never_writes(tmp_path: Path) -> None:
    # It must not "helpfully" repair the workspace: a local edit is possibly
    # intentional, and clobbering it is exactly what the caller must choose.
    ws = _workspace(tmp_path)
    install_skills(ws)
    edited = ws / ".claude/skills/horizon/SKILL.md"
    edited.write_text("mine\n", "utf-8")
    stale_skills(ws)
    assert edited.read_text("utf-8") == "mine\n"
