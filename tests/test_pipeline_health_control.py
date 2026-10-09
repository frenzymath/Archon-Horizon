from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.operations.health_control import ControlPlanSpec, HealthIssueSpec, HealthSnapshotSpec, Scope, apply_control_plan, capture_health_snapshot, propose_control_plan, supervision_activity, upsert_health_issue
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # Reuse isolated PostgreSQL fixtures.


def test_supervision_separates_journal_churn_from_work_and_scopes_owners(world):
    from archon_horizon.pipeline.execution.intent_reconciliation import JOURNAL_BLOCKER_PREFIX, INTENT_REPAIR_PREFIX

    run = world.run()
    world.disable_automations(run)
    owner = world.assignment(run)
    claim = world.claim()
    assert claim["assignment_id"] == str(owner["id"])
    create(world.conn, "provider_request", execution_id=claim["execution_id"],
           provider_thread_id=claim["provider_thread_record_id"], number=1,
           reason="assignment", status="completed")
    root = world.ledger(owner["id"])[0]
    resolved = change(world.conn, "obligation", root["id"], status="done",
                      resolution={"kind": "completed", "note": "Published the requested result"})
    work_time = resolved["updated_at"]
    for number in range(2, 202):
        create(world.conn, "obligation", assignment_id=owner["id"], number=number,
               kind="blocker", description=JOURNAL_BLOCKER_PREFIX + " (legacy wrapper)",
               status="done" if number < 201 else "open",
               resolution={"kind": "completed", "note": "Wrapper resolved"} if number < 201 else None)
    create(world.conn, "obligation", assignment_id=owner["id"], number=202,
           kind="decision", description=INTENT_REPAIR_PREFIX + "legacy-key", status="done",
           resolution={"kind": "completed", "note": "Repair accounted"})
    other_run = world.run()
    other_owner = world.assignment(other_run)
    activity = supervision_activity(world.conn, run["id"], [owner["id"], other_owner["id"]])
    assert set(activity) == {owner["id"]}
    assert activity[owner["id"]] == {
        "execution_count": 1, "last_execution_at": get(world.conn, "execution", claim["execution_id"])["started_at"],
        "completed_requests": 1, "journal_reconciliations": 200,
        "open_journal_blockers": 1, "last_work_accounted_at": work_time,
    }


def test_health_issue_observations_are_deduplicated_and_revisioned(world):
    run = world.run()
    spec = HealthIssueSpec(scope=Scope(project_id=world.project["id"], run_id=run["id"]),
                           code="provider.timeout", fingerprint="request-1",
                           summary="Provider request timed out")
    first = upsert_health_issue(world.conn, spec)
    second = upsert_health_issue(world.conn, spec.model_copy(update={"severity": "error"}))
    assert first["id"] == second["id"]
    assert second["revision"] == first["revision"] + 1
    assert second["occurrences"] == 2
    assert second["severity"] == "error"
    assert get(world.conn, "health_issue", first["id"])["status"] == "open"
    reopened = change(world.conn, "health_issue", first["id"], expected_revision=second["revision"],
                      status="resolved", resolved_at=func.now())
    observed = upsert_health_issue(world.conn, spec)
    assert observed["id"] == reopened["id"]
    assert observed["status"] == "open" and observed["resolved_at"] is None
    assert observed["occurrences"] == 3


def test_health_snapshots_are_immutable_and_deduplicated(world):
    run = world.run()
    spec = HealthSnapshotSpec(scope=Scope(run_id=run["id"]), kind="run", snapshot_key="frontier-1",
                              frontier_hash="a" * 64, health_hash="b" * 64,
                              payload={"queue": {"pending": 1}}, captured_at=datetime.now(timezone.utc))
    first = capture_health_snapshot(world.conn, spec)
    replay = capture_health_snapshot(world.conn, spec.model_copy(update={"payload": {"queue": {"pending": 9}}}))
    assert first["id"] == replay["id"]
    assert replay["payload"] == first["payload"]
    assert len(list(world.conn.execute(select(tables["health_snapshot"]).where(
        tables["health_snapshot"].c.run_id == run["id"])))) == 1


def test_control_plan_uses_run_revision_and_frontier_as_cas(world):
    run = world.run()
    create(world.conn, "run_coordination", run_id=run["id"], frontier_hash="f" * 64,
           last_progress_at=func.now(), checked_at=func.now())
    spec = ControlPlanSpec(project_id=world.project["id"], run_id=run["id"], plan_key="pause-provider",
                           expected_run_revision=run["revision"], expected_frontier_hash="f" * 64,
                           actions={"pause": {"assignment_ids": []}}, rationale="Storage pressure is rising")
    plan = propose_control_plan(world.conn, spec)
    replay = propose_control_plan(world.conn, spec)
    assert replay["id"] == plan["id"]
    applied = apply_control_plan(world.conn, plan["id"], expected_revision=plan["revision"])
    assert applied["status"] == "applied"
    with pytest.raises(DomainError) as error:
        apply_control_plan(world.conn, plan["id"], expected_revision=applied["revision"])
    assert error.value.code == "control_plan_not_applicable"

    second = propose_control_plan(world.conn, ControlPlanSpec(
        project_id=world.project["id"], run_id=run["id"], plan_key="resume-provider",
        expected_run_revision=run["revision"], expected_frontier_hash="f" * 64,
        actions={"resume": {"assignment_ids": []}}, rationale="Pressure cleared"))
    changed = change(world.conn, "run", run["id"], expected_revision=run["revision"], status="paused",
                     status_note="operator test")
    with pytest.raises(DomainError) as error:
        apply_control_plan(world.conn, second["id"], expected_revision=second["revision"])
    assert error.value.code == "control_plan_stale"
    assert get(world.conn, "run", run["id"])["revision"] == changed["revision"]
