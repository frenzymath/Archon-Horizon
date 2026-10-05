from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from archon_horizon.pipeline.connectors import ConnectorManager
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.records import change, create, get
from archon_horizon.pipeline.reviewer_invocations import ReviewerAttach, ReviewerReport, attach, prepare, report
from archon_horizon.pipeline.reviews import queue_review, review_readiness
from archon_horizon.pipeline.schema import tables
from archon_horizon.pipeline.worker_events import WorkerOperation, handle
from test_pipeline_review_assessments import assessment, deliver, setup
from test_pipeline_reviewer_invocations import observed, review, used  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


def native_event(world, review, status):
    observed(world, review, {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "wait",
        "agents_states": {"reporting-reviewer": {"status": status}}}})


def active_review(world, review):
    identity, item, policy = setup(world, review)
    prepared = prepare(world.conn, review["actor"], review["data"], world.service)
    attach(world.conn, review["actor"], prepared["provider_request_id"],
           ReviewerAttach(native_key="reporting-reviewer"))
    native_event(world, review, "running")
    return identity, item, policy, prepared


def publish(world, review, identity, prepared, key, *, historical=False):
    data = (ReviewerReport(verdict="commented", summary="Preserved incomplete findings", historical=True)
            if historical else ReviewerReport(verdict="approved", assessment=assessment()))
    operation = report(world.conn, review["actor"], prepared["provider_request_id"], data, world.service, key)
    delivered = deliver(world, review, identity, operation)
    return operation, delivered


def child_claims(world, request_id):
    claim = tables["resource_claim"]
    return [dict(row) for row in world.conn.execute(select(claim).where(
        claim.c.provider_request_id == request_id)).mappings()]


def replay_delivery(world, review, identity, operation, delivered):
    manager = ConnectorManager(None, world.service, lambda _: {})
    manager._record_review(world.conn, operation, dict(item=review["item"], actor=review["actor"],
        identity=identity, repository=get(world.conn, "repository", review["item"]["repository_id"])),
        {"id": delivered["remote_id"]})


@pytest.mark.parametrize("boundary", ["shared_provider", "parent_children"])
def test_delivered_report_keeps_native_reservation_and_enforces_capacity(world, review, boundary):
    if boundary == "shared_provider":
        change(world.conn, "resource_limit", review["limit"]["id"], max_concurrent=2)
    else:
        binding = tables["host_harness"]
        world.conn.execute(update(binding).where(binding.c.host_id == world.host["id"],
            binding.c.harness_id == world.harness["id"]).values(max_parallel_subagents=1))
    identity, item, policy, prepared = active_review(world, review)
    request_id = prepared["provider_request_id"]
    operation, delivered = publish(world, review, identity, prepared, "initial-report")
    replay_delivery(world, review, identity, operation, delivered)
    world.scheduler.tick(world.conn)
    request = get(world.conn, "provider_request", request_id)
    assert request["status"] == "running" and request["finished_at"] is None
    assert used(world, review) == 2
    claims = child_claims(world, request_id)
    assert len(claims) == 1 and claims[0]["released_at"] is None
    assert review_readiness(world.conn, item, policy)["ready"] is False
    other = create(world.conn, "forge_item", repository_id=item["repository_id"], remote_number=2,
        kind="pull_request", origin_run_id=review["run"]["id"], review_phase="postprocessing",
        target_branch="main", title="Other independent review", status="open", head_commit_oid="b" * 40,
        observed_at=datetime.now(timezone.utc))
    next_review = review["data"].model_copy(update={
        "forge_item_id": other["id"], "expected_head_oid": other["head_commit_oid"]})
    with pytest.raises(DomainError) as error:
        prepare(world.conn, review["actor"], next_review, world.service)
    assert error.value.code == "review_capacity_unavailable"
    native_event(world, review, "completed")
    assert prepare(world.conn, review["actor"], next_review, world.service)["status"] == "pending"


def test_amended_report_waits_for_native_final_before_gate_and_releases_exactly_once(world, review):
    identity, item, policy, prepared = active_review(world, review)
    request_id = prepared["provider_request_id"]
    publish(world, review, identity, prepared, "first-report")
    operation, amendment = publish(world, review, identity, prepared, "amended-report")
    native_event(world, review, "running")
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", request_id)["status"] == "running"
    assert used(world, review) == 2
    assert len(child_claims(world, request_id)) == 1
    assert child_claims(world, request_id)[0]["released_at"] is None
    assert review_readiness(world.conn, item, policy)["ready"] is False
    decision = dict(forge_item_id=item["id"], commit_oid=item["head_commit_oid"],
        verdict="approved", summary="Accept the completed independent review.")
    with pytest.raises(DomainError) as error:
        queue_review(world.conn, review["actor"], world.service, world.scheduler, decision, "too-early")
    assert error.value.code == "review_gate_blocked"

    native_event(world, review, "completed")
    finished = get(world.conn, "provider_request", request_id)
    released = child_claims(world, request_id)
    assert finished["status"] == "completed" and finished["finished_at"] is not None
    assert len(released) == 1 and released[0]["released_at"] is not None
    assert used(world, review) == 1
    assert review_readiness(world.conn, item, policy)["ready"] is True
    final = queue_review(world.conn, review["actor"], world.service, world.scheduler, decision, "final-decision")
    deliver(world, review, identity, final)
    assert get(world.conn, "outbox_operation", final["id"])["payload"]["gate_result"]["status"] == "accepted"

    native_event(world, review, "completed")
    native_event(world, review, "running")
    replay_delivery(world, review, identity, operation, amendment)
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", request_id)["finished_at"] == finished["finished_at"]
    assert child_claims(world, request_id) == released
    assert used(world, review) == 1


@pytest.mark.parametrize("status,historical", [
    ("failed", False), ("interrupted", False), ("failed", True), ("interrupted", True), ("completed", True)])
def test_failed_native_result_or_historical_delivery_cannot_manufacture_completion(world, review, status, historical):
    identity, item, policy, prepared = active_review(world, review)
    request_id = prepared["provider_request_id"]
    operation, delivered = publish(world, review, identity, prepared, "partial-report", historical=historical)
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", request_id)["status"] == "running"
    assert used(world, review) == 2
    native_event(world, review, status)
    terminal = get(world.conn, "provider_request", request_id)
    replay_delivery(world, review, identity, operation, delivered)
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", request_id)["status"] == status
    assert get(world.conn, "provider_request", request_id)["finished_at"] == terminal["finished_at"]
    assert used(world, review) == 1
    assert review_readiness(world.conn, item, policy)["ready"] is False


@pytest.mark.parametrize("confirmed", [False, True])
def test_report_delivery_does_not_replace_missing_native_final_on_parent_stop(world, review, confirmed):
    identity, item, policy, prepared = active_review(world, review)
    request_id = prepared["provider_request_id"]
    publish(world, review, identity, prepared, "delivered-before-stop")
    change(world.conn, "execution", review["execution"]["id"], status="succeeded" if confirmed else "lost",
        stop_confirmed_at=datetime.now(timezone.utc) if confirmed else None)
    world.scheduler.tick(world.conn)
    request = get(world.conn, "provider_request", request_id)
    assert request["status"] == ("interrupted" if confirmed else "running")
    assert (child_claims(world, request_id)[0]["released_at"] is not None) == confirmed
    if confirmed:
        assert request["failure"]["code"] == "execution_stop_confirmed"
    assert review_readiness(world.conn, item, policy)["ready"] is False


def test_old_lease_loss_retains_parent_and_child_claims_until_host_confirms_stop(world, review):
    change(world.conn, "resource_limit", review["limit"]["id"], max_concurrent=2)
    identity, item, policy, prepared = active_review(world, review)
    publish(world, review, identity, prepared, "report-before-lease-loss")
    request_id = prepared["provider_request_id"]
    execution_id = review["execution"]["id"]
    now = world.conn.execute(select(func.now())).scalar_one()
    old = now - timedelta(seconds=max(300, world.scheduler.config.lease_seconds * 2) + 60)
    change(world.conn, "execution", execution_id, lease_expires_at=old)
    world.scheduler.tick(world.conn)
    assert get(world.conn, "execution", execution_id)["status"] == "lost"
    change(world.conn, "execution", execution_id, finished_at=old)
    world.assignment(review["run"])
    claim = tables["resource_claim"]

    def reservations():
        return [dict(row) for row in world.conn.execute(select(claim).where(
            claim.c.execution_id == execution_id).order_by(claim.c.id)).mappings()]

    original = reservations()
    assert len(original) == 2 and all(row["released_at"] is None for row in original)
    for _ in range(2):
        world.scheduler.tick(world.conn)
        assert get(world.conn, "execution", execution_id)["stop_confirmed_at"] is None
        assert get(world.conn, "provider_request", request_id)["status"] == "uncertain"
        assert reservations() == original
        assert world.claim() is None
    assert review_readiness(world.conn, item, policy)["ready"] is False

    receipt = WorkerOperation(operation_id=uuid4(), execution_id=execution_id,
        epoch=review["execution"]["number"], kind="execution_finished",
        payload={"status": "lost", "reason": "Host confirmed process-group cleanup"},
        occurred_at=now.timestamp())
    handle(world.conn, world.host_actor, receipt, world.service, world.scheduler)
    stopped = get(world.conn, "execution", execution_id)
    released = reservations()
    assert stopped["stop_confirmed_at"] is not None
    assert get(world.conn, "provider_request", request_id)["status"] == "interrupted"
    assert all(row["released_at"] is not None for row in released)
    replay = receipt.model_copy(update={"operation_id": uuid4(), "occurred_at": now.timestamp() + 1})
    handle(world.conn, world.host_actor, replay, world.service, world.scheduler)
    world.scheduler.tick(world.conn)
    assert get(world.conn, "execution", execution_id)["stop_confirmed_at"] == stopped["stop_confirmed_at"]
    assert reservations() == released
    assert world.claim() is not None
