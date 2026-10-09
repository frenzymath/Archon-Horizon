from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
import subprocess
import threading

from archon_horizon.pipeline.worker.cache_retention import artifact_cache_lease, prune_native_outputs
from archon_horizon.pipeline.worker.lean_build import LeanBuildPolicy
from archon_horizon.pipeline.worker_config import LeanBuildConfig
from archon_horizon.pipeline.worker import build_engine
from archon_horizon.pipeline.worker import cache_retention


def artifact(root, name, *, age=0):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    # Retention counts physical allocation. GPFS may report zero blocks until
    # writes are durable; materialize the fixture before measuring its budget.
    with path.open('wb') as output:
        output.write(b"artifact" * 512)
        output.flush()
        os.fsync(output.fileno())
    os.utime(path, (time.time() - age, time.time() - age))
    return path


def test_budget_is_host_wide_and_retains_newest_artifact(tmp_path):
    root = tmp_path / "cache"
    first = artifact(root, "harness-a/lake/old", age=100)
    second = artifact(root, "harness-b/lake/new")
    budget = second.stat().st_blocks * 512
    with artifact_cache_lease(root, {"HORIZON_BUILD_CACHE_MAX_BYTES": str(budget)}):
        assert not first.exists()
        assert second.exists()


def test_expiry_preserves_git_worktrees_and_external_symlink_targets(tmp_path):
    root = tmp_path / "cache"
    expired = artifact(root, "worker/lake/expired", age=100)
    git = artifact(root, "worker/git/objects/borrowed", age=100)
    workspace = artifact(root, "workspaces/source.lean", age=100)
    external = artifact(tmp_path, "external/unique-source.lean", age=100)
    (root / "worker/lake/external").symlink_to(external.parent, target_is_directory=True)
    (root / "worker/lake/source.lean").symlink_to(external)
    with artifact_cache_lease(root, {"HORIZON_BUILD_CACHE_MAX_AGE_SECONDS": "10"}):
        assert not expired.exists()
        assert git.exists() and workspace.exists() and external.exists()
        assert (root / "worker/lake/source.lean").is_symlink()


def test_active_build_prevents_eviction_without_serializing_other_builds(tmp_path):
    root = tmp_path / "cache"
    env = {"HORIZON_BUILD_CACHE_MAX_BYTES": "0"}
    with artifact_cache_lease(root, env):
        active = artifact(root, "worker/lake/active")
        os.utime(root / ".artifact-retention-checked", (0, 0))
        def concurrent_build():
            with artifact_cache_lease(root, env):
                assert active.exists()
            assert active.exists()
        with ThreadPoolExecutor() as pool:
            pool.submit(concurrent_build).result(timeout=5)
        assert active.exists()
    assert not active.exists()


def test_retention_policy_roundtrips_into_provider_configuration(tmp_path):
    config = LeanBuildConfig(root=tmp_path, cache_max_bytes=4096, cache_max_age_seconds=60)
    policy = LeanBuildPolicy(**config.model_dump())
    assert policy.cache_max_bytes == 4096
    assert policy.cache_max_age_seconds == 60


def test_native_cleanup_preserves_sources_interfaces_recent_and_tracked_files(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    old = 8 * 86400
    generated = artifact(tmp_path, ".lake/build/ir/Fixture.c", age=old)
    tracked = artifact(tmp_path, ".lake/build/ir/Tracked.c", age=old)
    interface = artifact(tmp_path, ".lake/build/lib/lean/Fixture.olean", age=old)
    source = artifact(tmp_path, ".lake/build/ir/Source.lean", age=old)
    recent = artifact(tmp_path, ".lake/build/ir/Recent.c")
    subprocess.run(["git", "-C", str(tmp_path), "add", str(tracked)], check=True, capture_output=True)
    prune_native_outputs(tmp_path, {"HORIZON_BUILD_NATIVE_MAX_BYTES": "0"})
    assert not generated.exists()
    assert all(path.exists() for path in (tracked, interface, source, recent))


def test_native_cleanup_reclaims_stale_lean_ir_metadata(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    old = 8 * 86400
    metadata = artifact(tmp_path, ".lake/build/ir/PoincareLib/Fixture.json", age=old)
    digest = artifact(tmp_path, ".lake/build/ir/PoincareLib/Fixture.hash", age=old)
    recent = artifact(tmp_path, ".lake/build/ir/PoincareLib/Recent.json")
    prune_native_outputs(tmp_path, {"HORIZON_BUILD_NATIVE_MAX_BYTES": "0"})
    assert not metadata.exists() and not digest.exists()
    assert recent.exists()


def test_artifact_scans_observe_cadence_pressure_and_configuration(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cache_retention, "_prune", lambda *args, **kwargs: calls.append(kwargs) or True)
    with artifact_cache_lease(tmp_path, {}):
        pass
    with artifact_cache_lease(tmp_path, {}):
        pass
    assert len(calls) == 1
    env = {"HORIZON_BUILD_CACHE_MAX_BYTES": "1"}
    with artifact_cache_lease(tmp_path, env):
        pass
    assert len(calls) == 2
    monkeypatch.setattr(cache_retention.os, "statvfs", lambda path: type("Usage", (), {"f_bavail": 0, "f_frsize": 1})())
    with artifact_cache_lease(tmp_path, {**env, "HORIZON_BUILD_MINIMUM_FREE_BYTES": "1"}):
        pass
    assert len(calls) == 4


def test_native_scans_throttle_only_after_complete_success(tmp_path, monkeypatch):
    artifact(tmp_path, ".lake/build/ir/Fixture.c")
    calls = []
    complete = False
    def prune(*args):
        calls.append(args)
        return complete
    monkeypatch.setattr(cache_retention, "_prune_native", prune)
    prune_native_outputs(tmp_path, {})
    assert not (tmp_path / ".lake/horizon-native-retention.checked").exists()
    complete = True
    prune_native_outputs(tmp_path, {})
    prune_native_outputs(tmp_path, {})
    assert len(calls) == 2
    monkeypatch.setattr(cache_retention.os, "statvfs", lambda path: type("Usage", (), {"f_bavail": 0, "f_frsize": 1})())
    prune_native_outputs(tmp_path, {"HORIZON_BUILD_MINIMUM_FREE_BYTES": "1"})
    prune_native_outputs(tmp_path, {"HORIZON_BUILD_MINIMUM_FREE_BYTES": "1"})
    assert len(calls) == 4


def test_cleanup_io_failure_does_not_replace_build_success(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("concurrent cache removal")
    monkeypatch.setattr(cache_retention, "_prune", missing)
    monkeypatch.setattr(build_engine, "_command", lambda *args, **kwargs: 0)
    result = build_engine.run_check(tmp_path, env={"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache")})
    assert result["ok"]
    artifact(tmp_path, ".lake/build/ir/Fixture.c")
    monkeypatch.setattr(cache_retention, "_prune_native", missing)
    result = build_engine.run_check(tmp_path, env={"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache")})
    assert result["ok"]


def test_native_cleanup_rejects_symlinked_build_and_non_git_checkouts(tmp_path):
    external = artifact(tmp_path, "external/ir/Fixture.c", age=8 * 86400)
    checkout = tmp_path / "checkout"
    (checkout / ".lake").mkdir(parents=True)
    (checkout / ".lake/build").symlink_to(external.parent.parent, target_is_directory=True)
    prune_native_outputs(checkout, {"HORIZON_BUILD_NATIVE_MAX_BYTES": "0"})
    assert external.exists()
    unowned = artifact(tmp_path, "unowned/.lake/build/ir/Fixture.c", age=8 * 86400)
    prune_native_outputs(tmp_path / "unowned", {"HORIZON_BUILD_NATIVE_MAX_BYTES": "0"})
    assert unowned.exists()


def test_native_eviction_waits_for_active_checkout_build(tmp_path, monkeypatch):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    generated = artifact(tmp_path, ".lake/build/ir/Fixture.c", age=8 * 86400)
    entered, release = threading.Event(), threading.Event()
    def compiling(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return 0
    monkeypatch.setattr(build_engine, "_command", compiling)
    env = {"HORIZON_LEAN_CACHE_ROOT": str(tmp_path / "cache")}
    with ThreadPoolExecutor() as pool:
        active = pool.submit(build_engine.run_check, tmp_path, env=env)
        assert entered.wait(5)
        try:
            result = build_engine.run_check(tmp_path, queue_timeout=0,
                env={**env, "HORIZON_BUILD_NATIVE_MAX_BYTES": "0"})
            assert result["status"] == "deferred"
            assert generated.exists()
        finally:
            release.set()
        assert active.result(timeout=5)["ok"]
    assert build_engine.run_check(tmp_path, env={**env, "HORIZON_BUILD_NATIVE_MAX_BYTES": "0"})["ok"]
    assert not generated.exists()
