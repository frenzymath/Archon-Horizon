import json
import errno
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from archon_horizon.pipeline.worker import build_engine
from archon_horizon.pipeline.worker.lean_build import LeanBuildPolicy, check


@pytest.mark.parametrize("full_filesystem", ["source", "cache"])
def test_runtime_storage_guard_stops_entire_compiler_group_and_preserves_data(tmp_path, monkeypatch, full_filesystem):
    root, cache, tools = (tmp_path / name for name in ("source", "cache", "tools"))
    for path in (root, cache, tools):
        path.mkdir()
        (path / "retained").write_text("preserve")
    marker = tmp_path / "running.json"
    lake = tools / "lake"
    lake.write_text(f"#!{sys.executable}\n" + """
import json, os, signal, subprocess, sys, time
if '--no-build' in sys.argv:
    sys.exit(1)
child = subprocess.Popen([sys.executable, '-c',
    'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'], stdout=subprocess.PIPE)
child.stdout.readline()
with open(os.environ['BUILD_TEST_MARKER'], 'w') as out:
    json.dump([os.getpid(),child.pid], out)
time.sleep(60)
""")
    lake.chmod(0o700)
    selected = root if full_filesystem == "source" else cache
    monkeypatch.setattr(build_engine.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=50 if marker.exists() and Path(path) == selected else 1000))
    result = build_engine.run_check(root, ["Fixture"], timeout=10, minimum_free_bytes=100, env={
        "PATH": str(tools) + ":" + os.environ["PATH"], "HORIZON_LEAN_CACHE_ROOT": str(cache),
        "BUILD_TEST_MARKER": str(marker)})
    assert result["status"] == "deferred" and result["returncode"] == 75
    assert "storage reserve" in result["error"] and full_filesystem in result["error"]
    assert result["failure"] is None
    for pid in json.loads(marker.read_text()):
        for _ in range(50):
            stat = Path(f"/proc/{pid}/stat")
            # The process can disappear between exists() and read_text().
            try:
                state = stat.read_text().rsplit(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"Compiler process {pid} survived storage cancellation")
    assert (root / "retained").read_text() == (cache / "retained").read_text() == "preserve"


def test_managed_check_checks_source_disk_before_starting_toolchain(tmp_path, monkeypatch):
    source, cache = tmp_path / "source", tmp_path / "cache"
    source.mkdir()
    monkeypatch.setattr(build_engine.shutil, "disk_usage", lambda path: SimpleNamespace(free=0 if path == source else 1000))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("Started Lake below disk reserve"))
    result = check(source, [], LeanBuildPolicy(cache, minimum_free_bytes=100))
    assert result["status"] == "deferred" and result["returncode"] == 75
    assert "source filesystem" in result["error"]


def test_compiler_diagnostics_and_forwarded_output_are_bounded_without_tempfile(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_engine.tempfile, "TemporaryFile", lambda *args, **kwargs: pytest.fail("Unbounded diagnostic spool"))
    diagnostics = []
    command = [sys.executable, "-c", "import sys; sys.stdout.write('BEGIN'+('x'*2000000)+'END'); sys.exit(1)"]
    code = build_engine._command(command, tmp_path, dict(os.environ), time.monotonic() + 10, diagnostics=diagnostics)
    assert code == 1
    assert len(diagnostics[0]) <= 16400
    assert diagnostics[0].startswith("BEGIN") and diagnostics[0].endswith("END")
    output = capsys.readouterr().err
    assert len(output) < 263000 and "Further build output omitted" in output


@pytest.mark.parametrize("reserve", [-1, True, "invalid"])
def test_explicit_reserve_rejects_invalid_values(tmp_path, reserve):
    with pytest.raises(ValueError, match="reserve"):
        build_engine.run_check(tmp_path, minimum_free_bytes=reserve)


def test_preparation_timeout_stops_children_that_ignore_term(tmp_path):
    marker = tmp_path / "preparation.json"
    program = """
import json,os,subprocess,sys,time
child = subprocess.Popen([sys.executable,'-c',
    'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(60)'],stdout=subprocess.PIPE)
child.stdout.readline()
with open(sys.argv[1],'w') as out:
    json.dump([os.getpid(),child.pid,os.getsid(0)],out)
time.sleep(60)
"""
    with pytest.raises(TimeoutError):
        build_engine.captured_command([sys.executable, "-c", program, str(marker)], tmp_path,
                                      dict(os.environ), time.monotonic() + 0.5)
    parent, child, session = json.loads(marker.read_text())
    assert session == os.getsid(0)
    for pid in (parent, child):
        for _ in range(50):
            stat = Path(f"/proc/{pid}/stat")
            if not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"Preparation process {pid} survived timeout")


def test_captured_preparation_separates_stdout_and_stderr(tmp_path):
    result = build_engine.captured_command([sys.executable, "-c",
        "import sys;sys.stdout.buffer.write(b'file\\0name');sys.stderr.write('progress')"],
        tmp_path, dict(os.environ), time.monotonic() + 5)
    assert result.returncode == 0 and result.stdout == b"file\0name"
    assert result.stderr == b"progress"


def test_source_fingerprinting_honors_overall_deadline(tmp_path):
    (tmp_path / "lake-manifest.json").write_text('{"packages": []}')
    with pytest.raises(TimeoutError):
        build_engine.source_identity(tmp_path, {}, "Lake fixture", deadline=time.monotonic() - 1)


def test_storage_exhaustion_race_is_deferred_not_a_source_failure(tmp_path, monkeypatch):
    def full(*args, **kwargs):
        raise OSError(errno.ENOSPC, "full")
    monkeypatch.setattr(build_engine, "prepare_dependencies", full)
    result = build_engine.run_check(tmp_path, env={"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache")})
    assert result["returncode"] == 75 and result["status"] == "deferred" and result["failure"] is None
