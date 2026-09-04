"""Workspace-local scratch routing and safe stale cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

from archon_horizon.core.scratch import (
    clean_workspace_tmp,
    run_id_from_session_path,
    scratch_environment,
)
from archon_horizon.core.workspace import Workspace
from archon_horizon.cli import main


def _workspace(root: Path) -> Workspace:
    return Workspace(name="ws", root=root)


def _make_old(path: Path, *, mtime: float = 1.0) -> None:
    path.mkdir(parents=True, exist_ok=True)
    payload = path / "payload.bin"
    payload.write_bytes(b"scratch")
    os.utime(payload, (mtime, mtime))
    os.utime(path, (mtime, mtime))
    # The cleanup boundary is the run directory, so make its directory mtime
    # old as well (a fresh parent would correctly be retained as recently used).
    os.utime(path.parent, (mtime, mtime))


def test_scratch_environment_isolated_and_workspace_local(tmp_path: Path) -> None:
    scratch, env = scratch_environment(
        _workspace(tmp_path), run_id="0001", session="0002-horizon-T/unsafe", role="horizon"
    )

    assert scratch == tmp_path / ".archon-horizon" / "tmp" / "0001" / "0002-horizon-T-unsafe"
    assert scratch.is_dir()
    assert env["ARCHON_HORIZON_TMP_ROOT"] == str((tmp_path / ".archon-horizon" / "tmp").resolve())
    assert env["ARCHON_HORIZON_TMP"] == str(scratch.resolve())
    assert env["TMPDIR"] == env["TMP"] == env["TEMP"] == env["ARCHON_HORIZON_TMP"]
    assert run_id_from_session_path(
        tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions" / "s" / "subagents" / "child"
    ) == "0001"


def test_cleanup_dry_run_and_apply_protects_live_run(tmp_path: Path) -> None:
    state = tmp_path / ".archon-horizon"
    live = state / "tmp" / "0001" / "session"
    stale = state / "tmp" / "0002" / "session"
    _make_old(live)
    _make_old(stale)
    (state / "runs" / "0001").mkdir(parents=True)
    (state / "runs" / "0001" / "process.json").write_text(
        json.dumps({"pid": os.getpid()}), "utf-8"
    )

    preview = clean_workspace_tmp(state, older_than_s=10, now=1000, apply=False)
    assert preview.candidates == (".archon-horizon/tmp/0002",)
    assert preview.removed == ()
    assert preview.skipped_live_runs == ("0001",)
    assert stale.exists() and live.exists()

    applied = clean_workspace_tmp(state, older_than_s=10, now=1000, apply=True)
    assert applied.removed == (".archon-horizon/tmp/0002",)
    assert not stale.exists()
    assert live.exists()


def test_cleanup_rejects_negative_age(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="non-negative"):
        clean_workspace_tmp(tmp_path / ".archon-horizon", older_than_s=-1)


def test_tmp_cli_path_and_clean_json(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    assert main(["--root", str(ws), "init", "--no-interactive"]) == 0
    capsys.readouterr()

    assert main(["--root", str(ws), "tmp", "path", "--json"]) == 0
    path_payload = json.loads(capsys.readouterr().out)
    assert Path(path_payload["path"]) == ws / ".archon-horizon" / "tmp"

    stale = ws / ".archon-horizon" / "tmp" / "adhoc"
    _make_old(stale)
    assert main([
        "--root", str(ws), "tmp", "clean", "--older-than-hours", "0", "--json"
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert ".archon-horizon/tmp/adhoc" in preview["candidates"]
    assert stale.exists()

    assert main([
        "--root", str(ws), "tmp", "clean", "--older-than-hours", "0", "--apply", "--json"
    ]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["removed"] == [".archon-horizon/tmp/adhoc"]
    assert not stale.exists()
