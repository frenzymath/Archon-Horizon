"""Release version synchronization script."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "version.py"


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src/archon_horizon/frontend").mkdir(parents=True)
    (root / "demo/.archon-horizon").mkdir(parents=True)
    (root / "src/archon_horizon/__init__.py").write_text('__version__ = "0.1.0"\n', "utf-8")
    (root / "README.md").write_text("![Version](https://img.shields.io/badge/version-0.1.0-blue)\n", "utf-8")
    package = {"name": "dashboard", "version": "0.1.0"}
    lock = {"version": "0.1.0", "packages": {"": {"name": "dashboard", "version": "0.1.0"}}}
    (root / "src/archon_horizon/frontend/package.json").write_text(json.dumps(package), "utf-8")
    (root / "src/archon_horizon/frontend/package-lock.json").write_text(json.dumps(lock), "utf-8")
    (root / "demo/.archon-horizon/version").write_text("0.1.0\n", "utf-8")
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_set_version_updates_every_surface(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    assert _run(root, "1.2.3").returncode == 0
    assert _run(root, "--check").returncode == 0
    assert '__version__ = "1.2.3"' in (root / "src/archon_horizon/__init__.py").read_text("utf-8")
    assert "version-1.2.3-blue" in (root / "README.md").read_text("utf-8")
    assert json.loads((root / "src/archon_horizon/frontend/package-lock.json").read_text("utf-8"))["packages"][""]["version"] == "1.2.3"


def test_check_reports_drift(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    (root / "demo/.archon-horizon/version").write_text("9.9.9\n", "utf-8")
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "demo workspace: 9.9.9" in result.stderr
