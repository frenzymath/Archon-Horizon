"""Static export: sha256 endpoint files + SPA shell injection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.render.static_export import endpoint_key, export_static
from archon_horizon.server.service import WorkspaceService


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--name", "demo"])
    main(["--root", str(ws), "inbox", "add", "--kind", "hint", "--body", "x"])
    return ws


def test_endpoint_key_matches_sha256() -> None:
    assert endpoint_key("/api/state") == hashlib.sha256(b"/api/state").hexdigest()


def test_export_writes_hashed_endpoint_files(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)
    out = export_static(service, tmp_path / "out")  # no dist -> Python fallback shell

    state_file = out / "data" / "api" / f"{endpoint_key('/api/state')}.json"
    assert state_file.exists()
    assert json.loads(state_file.read_text())["workspace"] == "demo"
    assert (out / "index.html").exists()  # fallback HTML present


def test_export_with_spa_injects_static_marker(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body></body></html>", "utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", "utf-8")

    out = export_static(WorkspaceService(ws), tmp_path / "out", dist_dir=dist)
    index = (out / "index.html").read_text()
    assert "__ARCHON_STATIC__" in index           # flipped into static mode
    assert (out / "assets" / "app.js").exists()    # SPA assets copied
