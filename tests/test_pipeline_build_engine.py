"""Local Lake cache, dependency preparation, and checkout reservations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from archon_horizon.pipeline.worker import build_engine
from archon_horizon.pipeline.worker.build_engine import run_check, source_identity


@pytest.fixture
def lean_project(tmp_path):
    toolchain = os.environ.get("HORIZON_TEST_LEAN_TOOLCHAIN", "leanprover/lean4:v4.32.1")
    lake = shutil.which("lake")
    if not lake:
        pytest.skip("Lake integration requires an installed Lean toolchain")
    if not shutil.which("elan"):
        pytest.skip("Lake integration requires a preinstalled Elan toolchain")
    installed = subprocess.check_output(["elan", "toolchain", "list"], text=True, timeout=10)
    if toolchain not in {line.split()[0] for line in installed.splitlines() if line.strip()}:
        pytest.skip("integration tests do not download Lean toolchains")
    root = tmp_path / "source"
    root.mkdir()
    (root / "lean-toolchain").write_text(toolchain + "\n")
    (root / "lakefile.toml").write_text('name = "fixture"\ndefaultTargets = ["Fixture"]\n[[lean_lib]]\nname = "Fixture"\n')
    (root / "lake-manifest.json").write_text(json.dumps({"version": "1.1.0", "packagesDir": ".lake/packages", "packages": [], "name": "fixture", "lakeDir": ".lake"}))
    (root / ".gitignore").write_text(".lake/\n")
    (root / "Fixture").mkdir()
    (root / "Fixture" / "A.lean").write_text("def fixtureA : Nat := 42\n")
    (root / "Fixture.lean").write_text("import Fixture.A\nexample : fixtureA = 42 := rfl\n")
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    return root


def test_failed_local_check_includes_bounded_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(build_engine, "prepare_dependencies", lambda *args: None)

    def failed(command, *args, diagnostics=None, **kwargs):
        diagnostics.append("Fixture.lean:1:1: error: unknown identifier `missing`")
        return 1

    monkeypatch.setattr(build_engine, "_command", failed)
    result = run_check(tmp_path, ["Fixture"],
                       env={"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache")})
    assert result["status"] == "failed"
    assert result["failure"]["kind"] == "source_error"
    assert "Fixture.lean:1:1: error" in result["error"]
    assert "<checkout>" not in result["error"]


def test_input_identity_is_content_based_and_rejects_missing_dependencies(lean_project, tmp_path):
    env = {"LAKE_ARTIFACT_CACHE": "true"}
    key = source_identity(lean_project, env, "version")
    assert key
    (lean_project / "Fixture" / "A.lean").write_text("def fixtureA : Nat := 43\n")
    assert source_identity(lean_project, env, "version") != key
    assert source_identity(lean_project, env, "other-version") != key
    manifest = lean_project / "lake-manifest.json"
    data = json.loads(manifest.read_text())
    data["packages"] = [{"name": "external", "type": "path", "dir": "../external"}]
    manifest.write_text(json.dumps(data))
    assert source_identity(lean_project, env, "version") is None


def test_input_identity_includes_existing_local_path_dependencies(lean_project, tmp_path):
    dependency = tmp_path / "external"
    dependency.mkdir()
    (dependency / "Shared.lean").write_text("def sharedValue : Nat := 1\n")
    manifest = lean_project / "lake-manifest.json"
    data = json.loads(manifest.read_text())
    data["packages"] = [{"name": "external", "type": "path", "dir": "../external"}]
    manifest.write_text(json.dumps(data))
    key = source_identity(lean_project, {}, "version")
    assert key
    (dependency / "Shared.lean").write_text("def sharedValue : Nat := 2\n")
    assert source_identity(lean_project, {}, "version") != key


def test_dependency_checkout_accepts_a_matching_atomic_install_race(tmp_path, monkeypatch):

    dependency = tmp_path / "dependency"
    dependency.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=dependency, check=True, capture_output=True)
    (dependency / "Dep.lean").write_text("def value : Nat := 1\n")
    subprocess.run(["git", "add", "."], cwd=dependency, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@local",
                    "commit", "-m", "Dependency"], cwd=dependency, check=True, capture_output=True)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=dependency, text=True).strip()
    root = tmp_path / "project"
    root.mkdir()
    (root / "lake-manifest.json").write_text(json.dumps({"packagesDir": ".lake/packages", "packages": [{
        "name": "dep", "type": "git", "url": str(dependency), "rev": revision}]}))
    destination = root / ".lake/packages/dep"
    original_rename = build_engine.os.rename

    def raced_rename(source, target):
        if Path(target) == destination:
            shutil.copytree(source, target)
            raise OSError(errno.ENOTEMPTY, "Directory not empty")
        return original_rename(source, target)

    monkeypatch.setattr(build_engine.os, "rename", raced_rename)
    env = {"LAKE_CACHE_DIR": str(tmp_path / "cache/lake")}
    build_engine.prepare_dependencies(root, env, time.monotonic() + 30)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=destination, text=True).strip() == revision


def test_checkout_lock_prevents_competing_local_commands(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(build_engine, "prepare_dependencies", lambda *args: None)
    def command(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return 0
    monkeypatch.setattr(build_engine, "_command", command)
    env = {"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "host")}
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(run_check, tmp_path, env=env)
        try:
            assert entered.wait(2)
            second = run_check(tmp_path, env=env, queue_timeout=0)
            assert second["status"] == "deferred"
        finally:
            release.set()
        assert first.result()["ok"]


def test_probe_does_not_compile_cold_target(lean_project, tmp_path):
    result = run_check(lean_project, probe=True, timeout=30,
                       env={"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "host")})
    assert result["status"] == "deferred" and not result["ok"]
    assert not (lean_project / ".lake/build/lib/lean/Fixture.olean").exists()


@pytest.mark.parametrize("diagnostic", [
    "A.lean:1:0: error: object file 'B.olean' of module B does not exist",
    "A.lean:1:0: error: out of memory", "network connection failed",
    "A.lean:1:0: error: maximum number of heartbeats reached",
    "A.lean:1:0: error: Disk quota exceeded",
    "A.lean:1:0: error: No space left on device",
])
def test_transient_failure_is_not_source_failure(tmp_path, diagnostic):
    assert build_engine.source_failure(diagnostic, tmp_path, {}) is None


def test_build_diagnostics_redact_execution_credentials(tmp_path):
    output = f"{tmp_path}/A.lean:1:0: error: test-secret https://user:password@example.com/repo"
    env = {"HORIZON_EXECUTION_TOKEN": "test-secret"}
    failure = build_engine.source_failure(output, tmp_path, env)
    assert failure is not None
    for diagnostic in (failure["diagnostic"], build_engine.diagnostic_summary(output, tmp_path, env)):
        assert "test-secret" not in diagnostic and "password" not in diagnostic
        assert str(tmp_path) not in diagnostic


def test_local_build_does_not_use_api_credentials(tmp_path, monkeypatch):
    import urllib.request

    monkeypatch.setenv("HORIZON_API_URL", "https://must-not-contact.invalid")
    monkeypatch.setenv("HORIZON_EXECUTION_TOKEN", "test-secret")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("contacted API"))
    monkeypatch.setattr(build_engine, "prepare_dependencies", lambda *args: None)
    monkeypatch.setattr(build_engine, "_command", lambda *args, **kwargs: 0)
    result = run_check(tmp_path, env={"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache")})
    assert result["ok"]


def test_deferred_checks_have_an_execution_scoped_queue_budget(tmp_path, monkeypatch):
    waits = []

    def busy(root, env, deadline, progress):
        waits.append(progress.queue_timeout)
        raise build_engine.CheckDeferred("busy")

    monkeypatch.setattr(build_engine, "prepare_dependencies", busy)
    env = {"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache"), "HORIZON_EXECUTION_ID": "first"}
    assert run_check(tmp_path, env=env, queue_timeout=5)["status"] == "deferred"
    assert run_check(tmp_path, env=env, queue_timeout=5)["repeated_deferred"]
    assert not run_check(tmp_path, env={**env, "HORIZON_EXECUTION_ID": "second"}, queue_timeout=5)["repeated_deferred"]
    assert waits == [5, 0, 5]


def test_dependency_preparation_uses_a_shared_host_lane(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def preparing(*args):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(build_engine, '_prepare_dependencies', preparing)
    env = {'HORIZON_LEAN_CACHE_ROOT': str(tmp_path/'cache')}
    with ThreadPoolExecutor() as pool:
        first = pool.submit(build_engine.prepare_dependencies, tmp_path/'one', env, time.monotonic()+10)
        assert entered.wait(5)
        try:
            with pytest.raises(build_engine.CheckDeferred):
                build_engine.prepare_dependencies(tmp_path/'two', env, time.monotonic()+10,
                    build_engine.CheckProgress(0))
        finally:
            release.set()
        first.result(timeout=5)
