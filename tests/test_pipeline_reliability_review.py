"""Cross-module regressions found during the independent reliability audit."""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select, update

from archon_horizon.pipeline.persistence.records import create, get
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle

from test_pipeline_service import service_database, world


@pytest.mark.parametrize("status", ["pending", "uncertain", "failed"])
def test_completion_cannot_abandon_accepted_broker_intent(world, status):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    claim = world.claim()
    principal = world.conn.execute(select(tables["principal"]).where(
        tables["principal"].c.execution_id == UUID(claim["execution_id"]))).mappings().one()
    world.conn.execute(update(tables["obligation"]).where(
        tables["obligation"].c.assignment_id == assignment["id"]).values(
        status="done", resolution={"kind": "completed", "summary": "Work finished"}))
    item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=1,
                  kind="issue", title="Result", status="open", observed_at=datetime.now(timezone.utc))
    create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=principal["id"],
           kind="forge_comment", status=status, schema_version=1, idempotency_key="final-report",
           payload={"forge_item_id": str(item["id"]), "body": "Completed result and evidence"})
    findings = world.service.completion_findings(world.conn, assignment["id"])
    assert findings, "Completion would revoke delivery authority while its accepted report remains undelivered"


def test_rate_limit_cools_shared_provider_account_before_admitting_sibling(world):
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="account", max_concurrent=2)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"],
        harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    world.assignment(run)
    claim = world.claim()
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "failed",
                           {"kind": "provider", "code": "rate_limited", "message": "Shared account rate limit"})
    assert world.claim() is None, "A sibling must not immediately consume the same rate-limited provider account"


def provider_capacity(world):
    limit = create(world.conn, "resource_limit", kind="provider_account", slug="shared", max_concurrent=2)
    world.conn.execute(insert(tables["host_harness_limit"]).values(host_id=world.host["id"],
        harness_id=world.harness["id"], resource_limit_id=limit["id"]))
    run = world.run()
    world.disable_automations(run)
    for _ in range(4):
        world.assignment(run)
    return limit


def completed_request(world, grant):
    create(world.conn, "provider_request", provider_thread_id=UUID(grant["provider_thread_record_id"]),
           execution_id=UUID(grant["execution_id"]), number=1, reason="assignment", status="completed",
           finished_at=datetime.now(timezone.utc))


@pytest.mark.parametrize("failure", [
    {"kind": "execution", "code": "request_deadline", "message": "Provider request exceeded its execution deadline"},
    {"kind": "transport", "code": "timeout", "message": "Provider request exceeded its execution deadline"},
])
def test_local_session_deadline_keeps_shared_provider_capacity_available(world, failure):
    limit = provider_capacity(world)
    active, expired = world.claim(), world.claim()
    world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", expired["execution_id"]), "failed", failure)
    state = get(world.conn, "resource_limit", limit["id"])
    assert state["failure_count"] == 0 and state["cooldown_until"] is None
    assert get(world.conn, "execution", expired["execution_id"])["failure"]["code"] == "request_deadline"
    assert get(world.conn, "assignment", expired["assignment_id"])["status"] == "pending"
    assert world.claim() is not None, "An unrelated session may use the released slot"
    assert get(world.conn, "execution", active["execution_id"])["status"] == "running"


def test_upstream_timeout_still_cools_shared_provider_capacity(world):
    limit = provider_capacity(world)
    claim = world.claim()
    world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", claim["execution_id"]), "failed",
        {"kind": "transport", "code": "timeout", "message": "Provider HTTP request timed out"})
    assert get(world.conn, "resource_limit", limit["id"])["failure_count"] == 1
    assert world.claim() is None


def test_provider_cooldown_allows_only_one_recovery_probe(world):
    limit = provider_capacity(world)
    claim = world.claim()
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", claim["execution_id"]), "failed",
                           {"kind": "provider", "code": "rate_limited", "message": "Rate limited"})
    world.conn.execute(update(tables["resource_limit"]).where(tables["resource_limit"].c.id == limit["id"]).values(
        cooldown_until=func.now() - timedelta(seconds=1)))
    probe = world.claim()
    assert probe is not None
    assert world.claim() is None, "Half-open provider must admit a single probe"
    completed_request(world, probe)
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", probe["execution_id"]), "yielded")
    assert get(world.conn, "resource_limit", limit["id"])["failure_count"] == 0
    assert world.claim() is not None


def test_older_inflight_success_does_not_erase_new_provider_failure(world):
    limit = provider_capacity(world)
    older, failing = world.claim(), world.claim()
    completed_request(world, older)
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", failing["execution_id"]), "failed",
                           {"kind": "provider", "code": "provider_overloaded", "message": "Provider overloaded"})
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", older["execution_id"]), "yielded")
    assert get(world.conn, "resource_limit", limit["id"])["failure_count"] == 1
    assert world.claim() is None


def stopped_receipt(world, grant, *, status="yielded", native=None):
    return handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
        execution_id=UUID(grant["execution_id"]), epoch=grant["epoch"], kind="execution_finished",
        payload={"status": status, "provider_thread_id": native}, occurred_at=datetime.now(timezone.utc).timestamp()),
        world.service, world.scheduler)


def test_late_stop_receipt_does_not_finish_new_execution(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    first = world.claim()
    stopped_receipt(world, first, native="native-context")
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == assignment["id"]).values(not_before=None))
    second = world.claim()
    assert second and second["provider_thread_id"] == "native-context"
    active = get(world.conn, "execution", second["execution_id"])
    revision = get(world.conn, "assignment", assignment["id"])["revision"]
    stopped_receipt(world, first, status="cancelled", native="native-context")
    assert get(world.conn, "execution", second["execution_id"]) == active
    assert get(world.conn, "assignment", assignment["id"])["revision"] == revision
    assert get(world.conn, "assignment", assignment["id"])["status"] == "running"


def test_cancel_timeout_keeps_capacity_and_workspace_until_host_stop(world):
    from archon_horizon.pipeline.errors import DomainError

    limit = provider_capacity(world)
    grant = world.claim()
    assignment = get(world.conn, "assignment", grant["assignment_id"])
    request = create(world.conn, "provider_request", provider_thread_id=UUID(grant["provider_thread_record_id"]),
        execution_id=UUID(grant["execution_id"]), number=1, reason="assignment", status="running")
    world.command("cancel_assignment", assignment, note="Stop this execution")
    before = get(world.conn, "execution", grant["execution_id"])
    heartbeat = world.scheduler.heartbeat(world.conn, world.host_actor, UUID(grant["execution_id"]), grant["epoch"])
    assert heartbeat["stop"] is True
    assert get(world.conn, "execution", grant["execution_id"])["lease_expires_at"] == before["lease_expires_at"]
    world.conn.execute(update(tables["execution"]).where(tables["execution"].c.id == before["id"]).values(
        lease_expires_at=func.now() - timedelta(seconds=1)))
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", request["id"])["status"] == "uncertain"
    assert world.scheduler.available_workspace(world.conn, assignment, world.host["id"]) is None
    claim = world.conn.execute(select(tables["resource_claim"]).where(
        tables["resource_claim"].c.execution_id == before["id"], tables["resource_claim"].c.resource_limit_id == limit["id"])).mappings().one()
    assert claim["released_at"] is None
    with pytest.raises(DomainError, match="lease"):
        world.scheduler.heartbeat(world.conn, world.host_actor, before["id"], grant["epoch"])
    stopped_receipt(world, grant, status="cancelled", native="native-stopped")
    assert get(world.conn, "provider_request", request["id"])["status"] == "interrupted"
    assert get(world.conn, "resource_claim", claim["id"])["released_at"] is not None
    assert get(world.conn, "assignment", assignment["id"])["status"] == "cancelled"


def test_expired_execution_holds_workspace_even_between_provider_requests(world):
    from archon_horizon.pipeline.auth import Actor
    from archon_horizon.pipeline.commands import Command, execute
    from archon_horizon.pipeline.errors import DomainError

    limit = provider_capacity(world)
    grant = world.claim()
    execution = get(world.conn, "execution", grant["execution_id"])
    assignment = get(world.conn, "assignment", execution["assignment_id"])
    world.conn.execute(update(tables["provider_thread"]).where(
        tables["provider_thread"].c.id == UUID(grant["provider_thread_record_id"])).values(
        provider_thread_id="confirmed-native-context", status="available"))
    completed_request(world, grant)
    world.conn.execute(update(tables["execution"]).where(tables["execution"].c.id == execution["id"]).values(
        lease_expires_at=func.now() - timedelta(seconds=1)))
    world.scheduler.tick(world.conn)
    expired = get(world.conn, "execution", execution["id"])
    assert expired["stop_confirmed_at"] is None
    assert world.scheduler.available_workspace(world.conn, assignment, world.host["id"]) is None
    claim = world.conn.execute(select(tables["resource_claim"]).where(
        tables["resource_claim"].c.execution_id == execution["id"], tables["resource_claim"].c.resource_limit_id == limit["id"])).mappings().one()
    assert claim["released_at"] is None
    with pytest.raises(DomainError, match="physical"):
        world.command("confirm_host_stopped", expired, note="Lost host", evidence="No proof yet", machine_fenced=False)
    outsider = create(world.conn, "principal", kind="human", display_name="Project maintainer", username="review_operator")
    world.conn.execute(insert(tables["project_grant"]).values(principal_id=outsider["id"], project_id=world.project["id"], role="maintainer"))
    actor = Actor(outsider["id"], "human", {"username": "review_operator"}, "api_key")
    command = Command(operation="confirm_host_stopped", target_id=expired["id"], expected_revision=expired["revision"],
                      args={"note": "Verified host shut down", "evidence": "Hypervisor power-off receipt", "machine_fenced": True})
    with pytest.raises(DomainError):
        execute(world.conn, actor, command, world.service, world.scheduler)
    confirmed = execute(world.conn, world.actor, command, world.service, world.scheduler)
    assert confirmed["stop_confirmed_at"] is not None
    assert get(world.conn, "resource_claim", claim["id"])["released_at"] is not None
    assert world.scheduler.available_workspace(world.conn, assignment, world.host["id"])["id"] == execution["workspace_id"]
