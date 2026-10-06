"""Source releases preserve rebuild inputs and exclude workstation state."""

from __future__ import annotations

import io
from pathlib import Path
import runpy
import tarfile

import pytest


CHECKER = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_sdist.py"))


def source_archive(tmp_path: Path, *, extra: str | None = None, missing: str | None = None):
    root = tmp_path / "source"
    archive = tmp_path / "horizon.tar.gz"
    names = set(CHECKER["REQUIRED"]) - {missing}
    if extra:
        names.add(extra)
    with tarfile.open(archive, "w:gz") as target:
        for name in sorted(names):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"source: {name}\n")
            target.add(path, arcname=f"horizon-0.1.5/{name}")
        for name, payload in {
            "PKG-INFO": b"Metadata-Version: 2.4\n",
            "setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n",
        }.items():
            metadata = tarfile.TarInfo(f"horizon-0.1.5/{name}")
            metadata.size = len(payload)
            target.addfile(metadata, io.BytesIO(payload))
    return root, archive


def test_source_release_contains_current_rebuild_inputs(tmp_path):
    root, archive = source_archive(tmp_path)
    assert CHECKER["check_archive"](archive, root) == []


def test_source_release_requires_frontend_lockfile(tmp_path):
    missing = "src/archon_horizon/frontend/package-lock.json"
    root, archive = source_archive(tmp_path, missing=missing)
    assert f"missing: {missing}" in CHECKER["check_archive"](archive, root)


@pytest.mark.parametrize("extra", [
    ".mcp.json", ".codex/config.toml", ".claude/agents/old.md", "config.yaml",
    ".env", ".env.local", ".horizon/execution.sqlite-wal",
    "agent-library/digest/skills/horizon/SKILL.md", "lean-cache/helpers/build.py",
    "src/archon_horizon/frontend/node_modules/example/index.js", "build/lib/old.py",
])
def test_source_release_rejects_local_files(tmp_path, extra):
    root, archive = source_archive(tmp_path, extra=extra)
    assert f"local-only file: {extra}" in CHECKER["check_archive"](archive, root)


def test_source_release_rejects_stale_source(tmp_path):
    root, archive = source_archive(tmp_path)
    name = "src/archon_horizon/__init__.py"
    (root / name).write_text("changed after the archive was built\n")
    assert f"stale or unexpected source: {name}" in CHECKER["check_archive"](archive, root)
