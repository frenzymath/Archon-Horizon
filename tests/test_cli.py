"""CLI smoke: the command surface works against a scaffolded workspace."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main


def _run(root: Path, *argv: str) -> int:
    return main(["--root", str(root), *argv])


def _use_null_engines(ws: Path) -> None:
    """Swap the scaffolded claude/codex harnesses for the no-op engine so the
    test never shells out to a real CLI."""
    cfg = ws / "config.yaml"
    text = cfg.read_text("utf-8").replace("kind: claude-code", 'kind: "null"').replace(
        "kind: codex", 'kind: "null"'
    )
    cfg.write_text(text, "utf-8")


def test_full_cli_flow(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert _run(ws, "init", "--name", "demo") == 0
    assert (ws / "config.yaml").exists()
    assert (ws / ".archon-horizon" / "subagents").is_dir()
    assert (ws / ".claude" / "tools" / "archon-horizon-subagent.py").exists()
    _use_null_engines(ws)

    assert _run(ws, "project", "add", "ag-main", "projects/ag-main", "--build", "lake build") == 0
    assert (ws / "projects" / "ag-main").is_dir()

    assert _run(ws, "inbox", "add", "--kind", "hint", "--body", "affine first") == 0
    assert _run(ws, "inbox", "list") == 0
    assert _run(ws, "inbox", "complete", "I-0001") == 0

    assert _run(ws, "roadmap", "render") == 0
    assert (ws / ".archon-horizon" / "reports" / "roadmap.md").exists()

    assert _run(ws, "dashboard") == 0
    assert (ws / "dashboard" / "index.html").exists()

    assert _run(ws, "sync") == 0

    # Engines are not installed; dry-run must still plan without crashing.
    assert _run(ws, "run", "ag-main", "--rounds", "1", "--dry-run") == 0
    assert (ws / ".archon-horizon" / "runs" / "0001" / "run.yaml").exists()


def test_init_refuses_to_clobber(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    assert _run(ws, "init") == 0
    assert _run(ws, "init") == 1
