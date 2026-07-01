"""The workspace run lock.

Under ``exclusive=True`` the lock refuses a second *live* run (the strict
"duplicate Ground" guard). The orchestrator now acquires it advisorily
(``exclusive=False``): a second live run warns (``run.concurrent``) and proceeds
anyway. Either way a crashed run's stale lock is reclaimed by the next run.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.orchestration.locks import RunLockHeld, workspace_run_lock

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_run_lock_refuses_a_live_holder(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    # A lock owned by THIS (alive) process: a second acquire must refuse.
    lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "run_id": "0001"}))
    with pytest.raises(RunLockHeld):
        with workspace_run_lock(lock, run_id="0002"):
            pass
    # The live holder's lock is left intact, not stolen.
    assert json.loads(lock.read_text())["run_id"] == "0001"


def test_run_lock_steals_stale_and_releases(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    # A dead holder (a pid that cannot be alive) → stale, should be reclaimed.
    lock.write_text(json.dumps({"pid": 999_999_999, "host": socket.gethostname(), "run_id": "old"}))
    with workspace_run_lock(lock, run_id="new"):
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert not lock.exists()  # released on a clean exit


def test_run_lock_release_only_removes_own_lock(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    with workspace_run_lock(lock, run_id="a"):
        # Someone else's live lock lands on disk mid-run; we must not delete it.
        lock.write_text(json.dumps({"pid": os.getpid() + 1 if os.getpid() > 1 else 999, "host": "elsewhere"}))
    assert lock.exists()


def test_advisory_lock_proceeds_beside_a_live_holder(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    holder = {"pid": os.getpid(), "host": socket.gethostname(), "run_id": "0001"}
    lock.write_text(json.dumps(holder))
    # exclusive=False: proceed alongside the live run, reporting it, and never
    # touch its lock file.
    with workspace_run_lock(lock, run_id="0002", exclusive=False) as status:
        assert status.owned is False
        assert status.concurrent is not None
        assert status.concurrent["run_id"] == "0001"
    assert json.loads(lock.read_text())["run_id"] == "0001"


def test_orchestrator_warns_and_proceeds_on_concurrent_run(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(
        root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")}, inbox_providers=[local]
    )
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-1", title="x", projects=("ag-main",), status=RoadmapStatus.ACTIVE),))
    )
    # Simulate a live run already driving this workspace.
    lock = orch.workspace.state_path / "run.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "run_id": "0001"}))

    # The advisory lock lets the run proceed; it just records a warning event.
    orch.run(RunRecord(id="", rounds_requested=1))
    types = [event.type for event in orch.event_log.read_all()]
    assert "run.concurrent" in types
    # The pre-existing live holder's lock is left intact.
    assert json.loads(lock.read_text())["run_id"] == "0001"
