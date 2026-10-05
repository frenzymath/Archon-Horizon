from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import fcntl
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from archon_horizon.pipeline.worker import lean_build
from archon_horizon.pipeline.worker.lean_build import LeanBuildPolicy, check
from archon_horizon.pipeline.worker.provider import HeadlessAdapter
from archon_horizon.pipeline.worker.daemon import HarnessConfig, WorkerDaemon
from archon_horizon.pipeline.worker.contracts import ExecutionGrant
from archon_horizon.pipeline.worker.transport import WorkerTransport
from archon_horizon.pipeline.worker_config import WorkerConfig, load_worker
from archon_horizon.pipeline.worker import build_engine
from test_pipeline_build_engine import lean_project
from test_pipeline_worker import journal, repository


def config_values(tmp_path, *, container=False):
    root = tmp_path / "builds"
    token = tmp_path / "token"
    token.write_text("test-only-token")
    token.chmod(0o600)
    sandbox = {"mode": "unrestricted"}
    if container:
        sandbox = {"mode": "rootless_container", "image_digest": "image@sha256:" + "a" * 64,
                   "extra_mounts": [{"source": str(root), "target": "/horizon-build", "access": "read_write"}]}
    return {"host_id": str(uuid4()), "api_url": "http://localhost", "token_file": str(token),
            "journal_root": str(tmp_path / "journal"), "journal_min_free_bytes": 0,
            "workspace_roots": [str(tmp_path / "workspaces")],
            "lean_build": {"root": str(root), "max_parallel_builds": 2},
            "harnesses": [{"id": str(uuid4()), "adapter": "codex_exec", "executable": "/bin/false",
                           "provider_home": str(tmp_path / "home"), "scratch_root": str(tmp_path / "scratch"),
                           "sandbox": sandbox}]}


@pytest.mark.parametrize("container", [False, True])
def test_loaded_policy_uses_same_host_root_and_translates_container_path(tmp_path, container):
    values = config_values(tmp_path, container=container)
    path = tmp_path / "worker.json"
    path.write_text(json.dumps(values))
    daemon, _ = load_worker(path)
    try:
        config = next(iter(daemon.harnesses.values()))
        policy = config.lean_build
        assert policy.root == (Path("/horizon-build") if container else tmp_path / "builds")
        assert policy.max_parallel_builds == 2
        assert policy.artifact_cache
        assert (tmp_path / "builds").is_dir()
        assert config.environment == {}
        if container:
            assert config.sandbox.extra_mounts[0].source == tmp_path / "builds"
    finally:
        daemon.journal.close()
        daemon.transport.client.close()


@pytest.mark.parametrize("problem", ["missing_mount", "readonly", "wrong_source", "overlap", "symlink_overlap"])
def test_config_rejects_unshared_or_protected_build_storage(tmp_path, problem):
    values = config_values(tmp_path, container=True)
    mounts = values["harnesses"][0]["sandbox"]["extra_mounts"]
    if problem == "missing_mount":
        mounts.clear()
    elif problem == "readonly":
        mounts[0]["access"] = "read_only"
    elif problem == "wrong_source":
        mounts[0]["source"] = str(tmp_path / "another-root")
    elif problem == "overlap":
        values["lean_build"]["root"] = values["journal_root"]
    else:
        (tmp_path / "builds").symlink_to(tmp_path / "journal")
    with pytest.raises(ValidationError):
        WorkerConfig.model_validate(values)


def test_host_native_sandbox_retains_intent_and_build_writable_roots(tmp_path):
    adapter = HeadlessAdapter("codex_exec", "codex")
    args = adapter.command(agent_state_path=tmp_path / "intents", tool_writable_roots=(tmp_path / "builds",))
    roots = [arg.split("=", 1)[1] for arg in args if arg.startswith("sandbox_workspace_write.writable_roots=")]
    assert len(roots) == 1
    assert json.loads(roots[0]) == [str(tmp_path / "intents"), str(tmp_path / "builds")]
    with pytest.raises(ValueError):
        adapter.command(tool_writable_roots=(Path("/"),))
    with pytest.raises(ValueError):
        replace(adapter, sandbox_mode="read_only").command(tool_writable_roots=(tmp_path,))


def test_lean_check_rejects_source_changed_during_build(tmp_path, monkeypatch):
    policy = LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=0)
    monkeypatch.setattr(build_engine, "captured_command", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"Lake fixture"))
    snapshots = iter(["before", "after"])
    monkeypatch.setattr(build_engine, "source_identity", lambda *args, **kwargs: next(snapshots))
    def run(root, targets, **options):
        assert options["env"]["LAKE_ARTIFACT_CACHE"] == "true"
        return {"ok": True, "returncode": 0, "status": "passed"}
    monkeypatch.setattr(build_engine, "run_check", run)
    result = check(tmp_path, ["Fixture"], policy)
    assert result["status"] == "deferred" and result["returncode"] == 75
    assert not result["ok"] and not result["snapshot_verified"]


def test_storage_reserve_defers_without_starting_compiler(tmp_path, monkeypatch):
    policy = LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=2**63)
    monkeypatch.setattr(lean_build.subprocess, "run", lambda *args, **kwargs: pytest.fail("started compiler"))
    assert check(tmp_path, [], policy)["status"] == "deferred"
    with pytest.raises(ValueError):
        check(tmp_path, [], policy, lean_file="../outside.lean")


def test_cli_without_policy_fails_clearly(monkeypatch, capsys):
    monkeypatch.delenv("HORIZON_LEAN_BUILD", raising=False)
    assert lean_build.main([]) == 1
    result = json.loads(capsys.readouterr().out)
    assert "no managed Lean build policy" in result["error"]
    assert result["timings"]["total_seconds"] >= 0


def test_check_timings_include_fingerprints_outside_engine_and_keep_cached_build_time(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(lean_build.time, "monotonic", lambda: clock[0])
    def version(*args, **kwargs):
        clock[0] += 3
        return subprocess.CompletedProcess(args, 0, b"Lake fixture")
    def identity(*args, **kwargs):
        clock[0] += 2
        return "unchanged"
    def run(*args, **kwargs):
        clock[0] += 5
        return {"ok": True, "status": "passed", "returncode": 0,
                "duration_seconds": 5, "timings": {"build_seconds": 0, "queue_seconds": 1}}
    monkeypatch.setattr(build_engine, "captured_command", version)
    monkeypatch.setattr(build_engine, "source_identity", identity)
    monkeypatch.setattr(build_engine, "run_check", run)
    result = check(tmp_path, ["Fixture"], LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=0))
    assert result["timings"] == {"build_seconds": 0, "queue_seconds": 1, "preparation_seconds": 3,
                                 "source_fingerprint_seconds": 4, "engine_seconds": 5, "total_seconds": 12}
    assert result["duration_seconds"] == 5


@pytest.mark.parametrize("error,status", [(TimeoutError(), "timed_out"), (KeyboardInterrupt(), "cancelled"),
    (build_engine.CheckDeferred("busy"), "deferred"), (OSError(errno.ENOSPC, "full"), "deferred")])
def test_failed_fingerprint_cost_is_kept_for_deadlines_cancellation_and_deferral(tmp_path, monkeypatch, error, status):
    clock = [0.0]
    monkeypatch.setattr(lean_build.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(build_engine, "captured_command", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, b"Lake fixture"))
    def identity(*args, **kwargs):
        clock[0] += 3
        raise error
    monkeypatch.setattr(build_engine, "source_identity", identity)
    monkeypatch.setattr(build_engine, "run_check", lambda *args, **kwargs: pytest.fail("started after fingerprint failure"))
    result = check(tmp_path, [], LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=0))
    assert result["status"] == status
    assert result["timings"]["source_fingerprint_seconds"] == 3
    assert result["timings"]["total_seconds"] == 3


def test_real_lake_reuses_local_build_and_rechecks_changed_source(lean_project, tmp_path):
    policy = LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=0)
    first = check(lean_project, ["Fixture"], policy)
    assert first["ok"] and first["snapshot_verified"]
    second = check(lean_project, ["Fixture"], policy)
    assert second["ok"] and second["timings"]["build_seconds"] == 0
    assert first["source_key"] == second["source_key"]
    (lean_project / "Fixture.lean").write_text("import Fixture.A\nexample : False := by trivial\n")
    changed = check(lean_project, ["Fixture"], policy)
    assert not changed["ok"] and changed["source_key"] != first["source_key"]
    assert changed["failure"]["kind"] == "source_error"


def test_real_lake_queue_defers_and_recovers_after_slot_release(lean_project, tmp_path):
    policy = LeanBuildPolicy(tmp_path / "builds", queue_timeout_seconds=0.05, minimum_free_bytes=0)
    slots = policy.root / "slots"
    slots.mkdir(parents=True)
    with (slots / "0.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        result = check(lean_project, ["Fixture"], policy)
        assert result["status"] == "deferred" and not result["ok"]
    assert check(lean_project, ["Fixture"], policy)["ok"]


def test_real_lake_concurrent_checks_share_checkout_and_build_once(lean_project, tmp_path):
    policy = LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=0)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: check(lean_project, ["Fixture"], policy), range(2)))
    assert all(result["ok"] for result in results)
    assert sum(result["timings"]["build_seconds"] > 0 for result in results) == 1


def test_cache_namespaces_share_slots_but_not_artifact_directories(tmp_path):
    first = build_engine.build_environment(tmp_path, {"HORIZON_BUILD_CACHE_NAMESPACE": "harness-first"})
    second = build_engine.build_environment(tmp_path, {"HORIZON_BUILD_CACHE_NAMESPACE": "harness-second"})
    assert first["HORIZON_LEAN_CACHE_ROOT"] == second["HORIZON_LEAN_CACHE_ROOT"]
    assert first["LAKE_CACHE_DIR"] != second["LAKE_CACHE_DIR"]
    with pytest.raises(ValueError):
        build_engine.build_environment(tmp_path, {"HORIZON_BUILD_CACHE_NAMESPACE": "../escape"})


@pytest.mark.parametrize("cancel", [False, True])
def test_cli_timeout_and_cancellation_stop_compiler_process_group(tmp_path, cancel):
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "pids.json"
    lake = tools / "lake"
    lake.write_text(f"#!{sys.executable}\n" + """
import json, os, subprocess, sys, time
if '--version' in sys.argv:
    print('Lake fixture')
    sys.exit(0)
if '--no-build' in sys.argv:
    sys.exit(1)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
with open(os.environ['LEAN_TEST_PID_FILE'], 'w') as out:
    json.dump([os.getpid(), child.pid], out)
time.sleep(60)
""")
    lake.chmod(0o700)
    policy = LeanBuildPolicy(tmp_path / "builds", timeout_seconds=10 if cancel else 1, minimum_free_bytes=0)
    env = {**os.environ, **policy.environment("test"), "LEAN_TEST_PID_FILE": str(marker),
           "PATH": str(tools) + ":" + os.environ["PATH"]}
    process = subprocess.Popen([sys.executable, "-B", "-m", "archon_horizon.pipeline.worker.lean_build",
                                "--root", str(tmp_path)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        if cancel:
            process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        result = json.loads(stdout)
        assert process.returncode == (130 if cancel else 124), stderr.decode()
        assert result["status"] == ("cancelled" if cancel else "timed_out")
        assert not result["ok"]
        for pid in json.loads(marker.read_text()):
            stat = Path(f"/proc/{pid}/stat")
            assert not stat.exists() or stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        with (policy.root / "slots" / "0.lock").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)


def test_daemon_passes_managed_policy_to_provider(journal, repository, tmp_path):
    marker = tmp_path / "provider-observed.json"
    program = tmp_path / "provider"
    program.write_text(f"#!{sys.executable}\n" +
        "import json, os, sys\nsys.stdin.read()\n" +
        f"with open({str(marker)!r}, 'w') as out:\n" +
        " json.dump({'policy': json.loads(os.environ['HORIZON_LEAN_BUILD']), 'args': sys.argv, " +
        "'namespace': os.environ['HORIZON_BUILD_CACHE_NAMESPACE']}, out)\n" +
        "print(json.dumps({'type': 'turn.completed'}))\n")
    program.chmod(0o700)
    policy = LeanBuildPolicy(tmp_path / "builds", minimum_free_bytes=0)
    policy.root.mkdir()
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", str(repository), "repo", "Check Lean")
    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"execution": grant.__dict__})
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(200, json={"lease_seconds": 60, "continue": False})
        return httpx.Response(200, json={"acknowledged": True})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        transport = WorkerTransport("http://testserver", "test-token", client=client)
        daemon = WorkerDaemon(host_id="host", journal=journal, transport=transport,
            harnesses={"harness": HarnessConfig(HeadlessAdapter("codex_exec", str(program)),
                tmp_path / "home", tmp_path / "scratch", unrestricted=True, lean_build=policy)},
            workspace_roots=(repository,))
        assert daemon.run_once() == "succeeded"
    observed = json.loads(marker.read_text())
    assert observed["policy"]["root"] == str(policy.root)
    assert observed["namespace"] == "host"
    roots = [arg.split("=", 1)[1] for arg in observed["args"] if arg.startswith("sandbox_workspace_write.writable_roots=")]
    assert str(policy.root) in json.loads(roots[0])
