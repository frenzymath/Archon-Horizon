from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import update

from archon_horizon.pipeline import watchdog
from archon_horizon.pipeline.records import get
from archon_horizon.pipeline.schema import tables
from archon_horizon.pipeline.worker.provider import process_identity
from test_pipeline_service import service_database, world


def test_worker_watchdog_detects_one_deadlocked_lane_despite_other_progress(tmp_path):
    config = SimpleNamespace(journal_root=tmp_path)
    progress = {"schema_version": 1, "pid": os.getpid(), "process_identity": process_identity(os.getpid()),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(), "updated_monotonic": 1000,
        "components": {"publisher": {"monotonic": 1000, "deadline_seconds": 120},
                       "worker-0": {"monotonic": 800, "deadline_seconds": 120}}}
    (tmp_path / "worker-progress.json").write_text(json.dumps(progress))
    calls = []
    runner = lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0)
    for index in range(3):
        result = watchdog.check_worker(config, service="horizon-pipeline-worker.service", runner=runner,
            now=1000 + index, monotonic=1000)
    assert not result["healthy"] and result["restart_requested"]
    assert "worker-0" in result["reason"]
    assert calls == [["systemctl", "--user", "--no-block", "try-restart", "horizon-pipeline-worker.service"]]
    assert not watchdog.check_worker(config, service="horizon-pipeline-worker.service", runner=runner,
        now=1004, monotonic=1000)["restart_requested"]
    progress["components"]["worker-0"]["monotonic"] = 1000
    (tmp_path / "worker-progress.json").write_text(json.dumps(progress))
    assert watchdog.check_worker(config, service="horizon-pipeline-worker.service", runner=runner,
        now=1005, monotonic=1000)["healthy"]


def test_worker_watchdog_rejects_progress_from_another_process(tmp_path):
    config = SimpleNamespace(journal_root=tmp_path)
    (tmp_path / "worker-progress.json").write_text(json.dumps({"pid": os.getpid(), "process_identity": "old"}))
    result = watchdog.check_worker(config, service="horizon-pipeline-worker.service", now=1000)
    assert not result["healthy"] and not result["restart_requested"]


def test_pending_assignment_explains_disabled_compatible_host(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    assert world.scheduler.admission_blocker(world.conn, assignment) is None
    world.conn.execute(update(tables["host_harness"]).values(enabled=False))
    assert "No healthy enabled host" in world.scheduler.admission_blocker(world.conn, assignment)


def test_run_maintenance_scopes_the_phase_destination(world):
    run = world.run()
    automation = tables["automation"]
    from sqlalchemy import select
    row = world.conn.execute(select(automation).where(automation.c.run_id == run["id"],
        automation.c.name == "maintainer")).mappings().one()
    assert row["start_condition"]["expression"]["repository_ids"] == [str(world.document["source_repository_id"])]


def test_local_timeout_retries_without_disabling_host(world):
    from sqlalchemy import select
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    assert grant
    result = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", grant["execution_id"]), "failed",
        {"kind": "transport", "code": "local_operation_timeout", "message": "Snapshot timed out"})
    assert result["status"] == "pending" and result["retry_at"] is not None
    assert result["recovery_attempts"] == 1
    assert world.conn.execute(select(tables["host_harness"].c.enabled)).scalar_one()
