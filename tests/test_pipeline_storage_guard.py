from pathlib import Path

import httpx
import pytest

from archon_horizon.pipeline.worker.daemon import HarnessConfig, WorkerDaemon
from archon_horizon.pipeline.worker.journal import DurableJournal
from archon_horizon.pipeline.worker.provider import HeadlessAdapter
from archon_horizon.pipeline.worker.transport import WorkerTransport
from archon_horizon.pipeline.worker.storage_guard import inspect_storage
from archon_horizon.pipeline.config import StoragePolicy
from archon_horizon.pipeline import storage


class Usage:
    def __init__(self, total: int, free: int):
        self.total = total
        self.free = free
        self.used = total - free


def test_control_plane_pressure_keeps_running_below_cleanup_target(tmp_path, monkeypatch):
    monkeypatch.setattr(storage.shutil, "disk_usage", lambda path: Usage(1_000, 150))
    policy = StoragePolicy(minimum_free_bytes=100, cleanup_target_free_percent=20,
                           warn_used_percent=98, pause_used_percent=99)

    health = storage.pressure(tmp_path, policy)

    assert health["status"] == "ready"
    assert health["required_free_bytes"] == 100
    assert health["cleanup_target_free_percent"] == 20


def test_inspect_storage_reports_cleanup_target_without_blocking_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "archon_horizon.pipeline.worker.storage_guard.shutil.disk_usage",
        lambda path: Usage(1_000, 199),
    )

    health = inspect_storage(
        [("journal", tmp_path / "journal"), ("workspace", tmp_path / "workspace")],
        cleanup_target_percent=20,
    )

    assert health["status"] == "ready"
    assert health["cleanup_target_percent"] == 20
    assert health["cleanup_target_bytes"] == 200
    assert health["cleanup_recommended"] is True
    assert [root["name"] for root in health["roots"]] == ["journal", "workspace"]
    assert all(root["required_free_bytes"] == 0 for root in health["roots"])
    assert all(root["cleanup_target_percent"] == 20 for root in health["roots"])


def test_inspect_storage_keeps_existing_byte_floor_and_accepts_missing_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "archon_horizon.pipeline.worker.storage_guard.shutil.disk_usage",
        lambda path: Usage(10_000, 2_500),
    )

    health = inspect_storage(
        [("missing", tmp_path / "not-created")],
        cleanup_target_percent=20,
        minimum_free_bytes=3_000,
    )

    assert health["status"] == "storage_pressure"
    assert health["roots"][0]["required_free_bytes"] == 3_000
    assert health["roots"][0]["cleanup_target_bytes"] == 2_000
    assert health["roots"][0]["path"] == str(tmp_path / "not-created")


@pytest.mark.parametrize("value", [-1, 91, True, 20.0])
def test_inspect_storage_rejects_invalid_reserve(value, tmp_path):
    with pytest.raises(ValueError, match="storage cleanup target percent"):
        inspect_storage([("root", Path(tmp_path))], cleanup_target_percent=value)


def test_worker_denies_claim_when_any_managed_root_is_under_reserve(tmp_path, monkeypatch):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(500)

    journal = DurableJournal(tmp_path / "journal", minimum_free_bytes=0)
    transport = WorkerTransport(
        "http://testserver",
        "token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    daemon = WorkerDaemon(
        host_id="host",
        journal=journal,
        transport=transport,
        harnesses={
            "harness": HarnessConfig(
                HeadlessAdapter("codex_exec", "/bin/true"),
                tmp_path / "provider-home",
                tmp_path / "scratch",
                unrestricted=True,
            )
        },
        workspace_roots=(tmp_path / "workspace",),
    )
    monkeypatch.setattr(daemon, "_flush", lambda: None)
    monkeypatch.setattr(
        daemon,
        "storage_health",
        lambda: {"status": "storage_pressure", "roots": [{"name": "journal"}]},
    )
    try:
        assert daemon.run_once() is None
        assert requests == []
    finally:
        transport.client.close()
        journal.close()
