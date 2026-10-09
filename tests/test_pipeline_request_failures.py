from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_service import service_database, world  # noqa: F401


FAILURE = {"kind": "transport", "code": "timeout", "message": "Provider request exceeded its execution deadline"}


@pytest.fixture
def request_state(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    request = create(world.conn, "provider_request", provider_thread_id=UUID(claim["provider_thread_record_id"]),
        execution_id=UUID(claim["execution_id"]), number=1, reason="assignment", status="running")
    return assignment, claim, request


def observe(world, claim, payload, *, kind="provider_observed"):
    operation = WorkerOperation(operation_id=uuid4(), execution_id=claim["execution_id"], epoch=claim["epoch"],
        kind=kind, occurred_at=datetime.now(timezone.utc).timestamp(), payload=payload)
    return handle(world.conn, world.host_actor, operation, world.service, world.scheduler)


def complete(world, claim, request, **payload):
    return observe(world, claim, {"event": "request_completed", "request_id": str(request["id"]),
        "provider_thread_record_id": str(request["provider_thread_id"]), **payload})


def failures(world, request):
    activity = tables["activity"]
    return list(world.conn.execute(select(activity).where(activity.c.provider_request_id == request["id"],
        activity.c.kind == "failure")).mappings())


def test_request_failure_persists_normalized_diagnostic_activity_and_terminal_history(world, request_state):
    assignment, claim, request = request_state
    complete(world, claim, request, status="failed", failure={**FAILURE, "diagnostic_artifact_id": None})
    recorded = get(world.conn, "provider_request", request["id"])
    assert recorded["status"] == "failed" and recorded["failure"] == FAILURE
    activity = failures(world, request)
    assert len(activity) == 1 and "transport/timeout" in activity[0]["summary"]
    assert FAILURE["message"] in activity[0]["summary"]
    assert activity[0]["assignment_id"] == assignment["id"]
    assert activity[0]["provider_thread_id"] == request["provider_thread_id"]
    for outcome in ("failed", "completed"):
        complete(world, claim, request, status=outcome,
            failure={"kind": "execution", "code": "later_failure", "message": "Must not overwrite history"})
        assert get(world.conn, "provider_request", request["id"]) == recorded
        assert failures(world, request) == activity


@pytest.mark.parametrize("status", ["succeeded", "completed"])
def test_successful_request_clears_stale_failure_and_ignores_supplied_failure(world, request_state, status):
    _, claim, request = request_state
    change(world.conn, "provider_request", request["id"], failure=FAILURE)
    complete(world, claim, request, status=status, failure=FAILURE)
    recorded = get(world.conn, "provider_request", request["id"])
    assert recorded["status"] == "completed" and recorded["failure"] is None
    assert failures(world, request) == []


def test_invalid_failure_does_not_persist_raw_extra_fields(world, request_state):
    _, claim, request = request_state
    with pytest.raises(ValidationError):
        complete(world, claim, request, status="failed", failure={**FAILURE, "raw_prompt": "private prompt"})
    assert get(world.conn, "provider_request", request["id"])["status"] == "running"
    assert failures(world, request) == []


@pytest.mark.parametrize("latest_status,existing_failure", [("failed", None), ("completed", None), ("failed", {
    "kind": "provider", "code": "rate_limited", "message": "Provider request failed: rate_limited"})])
def test_old_worker_stop_receipt_enriches_only_latest_failed_primary_request(world, request_state, latest_status, existing_failure):
    _, claim, first = request_state
    old = change(world.conn, "provider_request", first["id"], status="failed", finished_at=datetime.now(timezone.utc))
    latest = create(world.conn, "provider_request", provider_thread_id=first["provider_thread_id"],
        execution_id=first["execution_id"], number=2, reason="continuation", status=latest_status,
        failure=existing_failure, finished_at=datetime.now(timezone.utc))
    parent = get(world.conn, "provider_thread", first["provider_thread_id"])
    child = create(world.conn, "provider_thread", assignment_id=parent["assignment_id"], number=2, kind="child",
        parent_request_id=latest["id"], workspace_id=parent["workspace_id"],
        harness_revision_id=parent["harness_revision_id"], skill_bundle_artifact_id=parent["skill_bundle_artifact_id"],
        provider_state_ref="native-child", applied_model_options={}, status="available")
    child_request = create(world.conn, "provider_request", provider_thread_id=child["id"],
        execution_id=first["execution_id"], number=1, reason="continuation", status="failed")
    receipt = {"status": "failed", "provider_thread_id": "saved-native-context", "failure": FAILURE}
    observe(world, claim, receipt, kind="execution_finished")
    recorded = get(world.conn, "provider_request", latest["id"])
    expected = FAILURE if latest_status == "failed" and existing_failure is None else existing_failure
    assert recorded["failure"] == expected
    assert recorded["status"] == latest_status and recorded["finished_at"] == latest["finished_at"]
    assert get(world.conn, "provider_request", first["id"]) == old
    assert get(world.conn, "provider_request", child_request["id"]) == child_request
    assert len(failures(world, latest)) == int(latest_status == "failed" and existing_failure is None)
    observe(world, claim, receipt, kind="execution_finished")
    assert get(world.conn, "provider_request", latest["id"]) == recorded
    assert len(failures(world, latest)) == int(latest_status == "failed" and existing_failure is None)


def test_old_worker_stop_receipt_does_not_attribute_postprocessing_failure_to_provider(world, request_state):
    _, claim, request = request_state
    failed = change(world.conn, "provider_request", request["id"], status="failed", finished_at=datetime.now(timezone.utc))
    observe(world, claim, {"status": "failed", "failure": {
        "kind": "storage", "code": "storage_full", "message": "Checkpoint storage failed"}}, kind="execution_finished")
    assert get(world.conn, "provider_request", request["id"]) == failed
    assert failures(world, request) == []
