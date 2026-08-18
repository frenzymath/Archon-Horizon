from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from archon_horizon.cli import main
from archon_horizon.commands.check import check


_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def _workspace(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "ws"
    project = root / "projects" / "ag-main"
    project.mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    session = root / ".archon-horizon" / "runs" / "0001" / "sessions" / "s1"
    session.mkdir(parents=True)
    return root, project, session


def test_attempt_save_preserves_sources_and_manifest(tmp_path: Path, monkeypatch, capsys) -> None:
    root, project, session = _workspace(tmp_path)
    source = project / "Draft.lean"
    source.write_text("example : True := by\n  trivial\n", "utf-8")
    diagnostics = project / "diagnostics.txt"
    diagnostics.write_text("type mismatch\n", "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION_DIR", str(session))
    monkeypatch.setenv("ARCHON_HORIZON_NO_SYNC", "1")

    assert main([
        "--root", str(root), "attempt", "save", str(source),
        "--reason", "the induction hypothesis is too weak",
        "--diagnostics", str(diagnostics), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    attempt_dir = Path(payload["artifact_dir"])
    assert (attempt_dir / "files" / "projects" / "ag-main" / "Draft.lean").read_text("utf-8") == source.read_text("utf-8")
    assert (attempt_dir / "diagnostics.txt").read_text("utf-8") == "type mismatch\n"
    assert payload["files"][0]["lines"] == 2


def test_check_serializes_and_reuses_identical_concurrent_request(
    tmp_path: Path, monkeypatch,
) -> None:
    root, project, session = _workspace(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    lake = bin_dir / "lake"
    lake.write_text(
        "#!/bin/sh\nprintf 'call\\n' >> \"$CHECK_CALLS\"\nsleep 0.4\nexit 0\n",
        "utf-8",
    )
    lake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("CHECK_CALLS", str(calls))
    monkeypatch.setenv("ARCHON_HORIZON_SESSION_DIR", str(session))
    monkeypatch.chdir(project)
    ctx = SimpleNamespace(obj={"root": root})
    failures: list[BaseException] = []

    def run_check() -> None:
        try:
            check(ctx, targets=[], lean_file=None, timeout=10, as_json=False)
        except BaseException as exc:  # captured so thread failures reach pytest
            failures.append(exc)

    first = threading.Thread(target=run_check)
    second = threading.Thread(target=run_check)
    first.start()
    time.sleep(0.08)
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not failures
    assert not first.is_alive() and not second.is_alive()
    assert calls.read_text("utf-8").splitlines() == ["call"]
    results = [json.loads(path.read_text("utf-8")) for path in (session / "checks").glob("*.json")]
    assert results and any(result["status"] in {"passed", "reused"} for result in results)
