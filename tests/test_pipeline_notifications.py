from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from archon_horizon.pipeline.auth import authenticate
from archon_horizon.pipeline.communications import route_subject_event, subscribe
from archon_horizon.pipeline.connectors import ConnectorFailure, ConnectorManager, ForgejoClient
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.models import SubscriptionCreate
from archon_horizon.pipeline.notifications import (
    CONTROL_NOTICE_LIMIT, CONTROL_SUMMARY_BYTES, OperatorNotice, control_summary,
    delivery_failed, operator_notice, prompt_summary,
)
from archon_horizon.pipeline.prompts import goal
from archon_horizon.pipeline.records import change, create, emit, get
from archon_horizon.pipeline.schema import tables
from test_pipeline_service import service_database, world
from test_pipeline_api import api, api_database, auth, mutate


def assignment(world):
    run = world.run()
    world.disable_automations(run)
    return run, world.assignment(run)


def notices(world, assignment_id):
    return list(world.conn.execute(select(tables["notification"]).where(
        tables["notification"].c.assignment_id == assignment_id)).mappings())


def test_initial_and_continuation_include_control_without_resolving_it(world):
    run, assigned = assignment(world)
    notice = operator_notice(world.conn, world.actor, assigned["id"],
        OperatorNotice(message="Use the corrected theorem statement before proceeding."))
    for initial in (True, False):
        prompt = goal(world.conn, world.service, assigned, run, world.mission, initial=initial)
        assert "Use the corrected theorem statement" in prompt
        assert str(notice["id"]) in prompt
        assert "This summary does not acknowledge or resolve notices" in prompt
        assert ("Read $HORIZON_SKILLS_DIR" in prompt) is initial
    assert get(world.conn, "notification", notice["id"]) == notice
    assert "Handle 1 pending control notifications." in world.service.completion_findings(world.conn, assigned["id"])
    change(world.conn, "notification", notice["id"], disposition="handled", disposition_note="Corrected the goal")
    assert prompt_summary(world.conn, assigned["id"]) is None


def test_summary_bounds_unicode_and_preserves_overflow_and_assignment_scope(world):
    run, assigned = assignment(world)
    other = world.assignment(run)
    operator_notice(world.conn, world.actor, other["id"], OperatorNotice(message="Private other assignment"))
    created = [operator_notice(world.conn, world.actor, assigned["id"],
        OperatorNotice(message=f"Notice {number}: " + "\u03b1" * 3000)) for number in range(12)]
    summary = control_summary(world.conn, assigned["id"])
    assert 0 < len(summary["notices"]) <= CONTROL_NOTICE_LIMIT
    assert summary["pending"] == 12
    assert summary["omitted"] == 12 - len(summary["notices"])
    assert [row["id"] for row in summary["notices"]] == [str(row["id"]) for row in created[:len(summary["notices"])]]
    assert all(row["truncated"] for row in summary["notices"])
    prompt = prompt_summary(world.conn, assigned["id"])
    assert len(prompt.encode("utf-8")) <= CONTROL_SUMMARY_BYTES
    assert "Private other assignment" not in prompt
    assert all(row["disposition"] == "pending" and row["delivered_at"] is None for row in notices(world, assigned["id"]))
    context = world.service.context(world.conn, world.actor, assigned["id"])
    assert context["control_notices"] == summary


@pytest.mark.parametrize("role,functions", [("worker", []), ("maintainer", []), ("maintainer", ["orchestrator"])])
@pytest.mark.parametrize("status", ["succeeded", "yielded"])
def test_unhandled_notice_cannot_restart_successful_context_forever(world, role, functions, status):
    run = world.run(retry_policy={"max_no_progress_requests": 2})
    world.disable_automations(run)
    assigned = world.assignment(run, role=role, functions=functions)
    notice = operator_notice(world.conn, world.actor, assigned["id"],
        OperatorNotice(message="Inspect and handle the concrete recovery instruction."))
    grant = world.claim()
    for number in (1, 2):
        create(world.conn, "provider_request", provider_thread_id=UUID(grant["provider_thread_record_id"]),
               execution_id=UUID(grant["execution_id"]), number=number, reason="continuation", status="completed")
    result = world.scheduler.finish(world.conn, world.host_actor,
        get(world.conn, "execution", grant["execution_id"]), status)
    assert result["status"] == "failed"
    assert "reconsideration" in result["status_note"]
    assert get(world.conn, "notification", notice["id"])["disposition"] == "pending"
    assert world.claim() is None


def test_operator_notices_require_operator_scope_and_active_target(world):
    run, assigned = assignment(world)
    grant = world.claim()
    worker = authenticate(world.conn, grant["execution_token"])
    with pytest.raises(DomainError) as denied:
        operator_notice(world.conn, worker, assigned["id"], OperatorNotice(message="pretend operator"))
    assert denied.value.status == 403
    for invalid in ("  ", "hidden\x00value", 5):
        with pytest.raises(ValidationError):
            OperatorNotice(message=invalid)
    change(world.conn, "assignment", assigned["id"], status="completed")
    with pytest.raises(DomainError, match="pending or running"):
        operator_notice(world.conn, world.actor, assigned["id"], OperatorNotice(message="Do more"))
    assert not notices(world, assigned["id"])


@pytest.mark.parametrize("failure,attempts,expected", [
    (ConnectorFailure("unavailable", transient=True), 1, "pending"),
    (ConnectorFailure("lost_reply", uncertain=True), 30, "uncertain"),
    (ConnectorFailure("permission_denied"), 1, "failed"),
    (ConnectorFailure("unavailable", transient=True), 20, "failed"),
])
def test_actual_delivery_settlement_only_notifies_actionable_failure(world, failure, attempts, expected):
    run, assigned = assignment(world)
    grant = world.claim()
    actor = authenticate(world.conn, grant["execution_token"])
    manager = ConnectorManager(SimpleNamespace(transaction=lambda: nullcontext(world.conn)), world.service, lambda key: {})
    operation = create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=actor.id,
        kind="forge_comment", schema_version=1, idempotency_key=str(uuid4()), payload={}, status="running",
        retry_count=attempts, lease_epoch=1, lease_owner=manager.owner,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert manager._settle_operation(operation, None, None, failure) == expected
    rows = notices(world, assigned["id"])
    assert len(rows) == (1 if expected == "failed" else 0)
    if rows:
        settled = get(world.conn, "outbox_operation", operation["id"])
        delivery_failed(world.conn, settled)
        assert len(notices(world, assigned["id"])) == 1
        summary = control_summary(world.conn, assigned["id"])
        assert summary["notices"][0]["operation_url"].endswith(str(operation["id"]))
        assert "do not resend under a new key" in summary["notices"][0]["excerpt"]


def test_forge_subscriptions_are_exact_deduplicated_quiet_and_respect_lifecycle(world):
    run, assigned = assignment(world)
    item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=7,
        kind="pull_request", title="Proof", status="open", observed_at=datetime.now(timezone.utc))
    recipients = {"digest": assigned, "prompt": world.assignment(run), "muted": world.assignment(run),
                  "expired": world.assignment(run), "finished": world.assignment(run)}
    for mode, recipient in recipients.items():
        subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=recipient["id"],
            subject={"kind": "forge_item", "id": item["id"]},
            mode=mode if mode in {"digest", "prompt", "muted"} else "digest",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1) if mode == "expired" else None), world.service)
    change(world.conn, "assignment", recipients["finished"]["id"], status="completed")
    unrelated = world.assignment(run)
    event = emit(world.conn, None, world.project["id"], "forge_item", item, ["head_commit_oid"])
    route_subject_event(world.conn, event)
    route_subject_event(world.conn, event)
    for mode, recipient in recipients.items():
        rows = notices(world, recipient["id"])
        assert len(rows) == (1 if mode in {"digest", "prompt"} else 0)
        if rows:
            assert rows[0]["urgency"] == ("direct" if mode == "prompt" else "routine")
        assert prompt_summary(world.conn, recipient["id"]) is None
    assert not notices(world, unrelated["id"])
    assert get(world.conn, "assignment", recipients["finished"]["id"])["status"] == "completed"


def test_forge_sync_routes_changed_items_once_without_prompt_noise(world, monkeypatch):
    run, assigned = assignment(world)
    item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=7,
        kind="pull_request", title="Proof", status="open", head_commit_oid="a" * 40,
        target_branch="main", observed_at=datetime.now(timezone.utc))
    subscribe(world.conn, world.actor, SubscriptionCreate(assignment_id=assigned["id"],
        subject={"kind": "forge_item", "id": item["id"]}), world.service)
    remote = {"number": 7, "_kind": "pull_request", "title": "Proof", "state": "open",
              "head": {"sha": "b" * 40}, "base": {"ref": "main"}, "labels": []}
    monkeypatch.setattr(ForgejoClient, "sync_items", lambda self, path: [remote] if path == "workspace" else [])
    manager = ConnectorManager(SimpleNamespace(transaction=lambda: nullcontext(world.conn)), world.service,
                               lambda key: {"token": "fixture-only-token"})
    integration = get(world.conn, "integration", world.workspace_repo["integration_id"])
    manager.sync_forge(integration)
    manager.sync_forge(integration)
    assert len(notices(world, assigned["id"])) == 1
    assert prompt_summary(world.conn, assigned["id"]) is None


def test_operator_notice_api_is_idempotent_reauthorizes_and_exposes_readable_content(api):
    client, database, world, run, operator_token, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        assigned = world.assignment(run)
        grant = world.claim()
    path = f"/api/v3/assignments/{assigned['id']}/control-notices"
    payload, key = {"message": "Update the statement before more proof work."}, str(uuid4())
    created = mutate(client, path, payload, operator_token, key)
    assert created.status_code == 200, created.text
    assert mutate(client, path, payload, operator_token, key).json() == created.json()
    assert mutate(client, path, payload, grant["execution_token"]).status_code == 403
    context = client.get(f"/api/v3/assignments/{assigned['id']}/context", headers=auth(grant["execution_token"]))
    notice = context.json()["control_notices"]["notices"][0]
    event = client.get(notice["detail_url"], headers=auth(grant["execution_token"]))
    assert event.status_code == 200, event.text
    assert event.json()["event"]["payload"]["message"] == payload["message"]
    with database.transaction() as conn:
        conn.execute(tables["system_grant"].delete().where(tables["system_grant"].c.principal_id == world.actor.id))
        conn.execute(tables["project_grant"].update().where(tables["project_grant"].c.principal_id == world.actor.id,
            tables["project_grant"].c.project_id == world.project["id"]).values(role="viewer"))
    assert mutate(client, path, payload, operator_token, key).status_code == 403


def test_control_notice_read_is_bounded_scoped_live_authorized_and_never_acknowledges(api, tmp_path):
    from test_pipeline_service import make_world

    client, database, world, run, operator_token, host_token = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        assigned = world.assignment(run)
        grant = world.claim()
        for number in range(12):
            operator_notice(conn, world.actor, assigned["id"], OperatorNotice(message=f"Notice {number}: " + "\u03b1" * 3000))
        other = make_world(conn, tmp_path / "other")
        other_run = other.run()
        other.disable_automations(other_run)
        other_assigned = other.assignment(other_run)
        operator_notice(conn, other.actor, other_assigned["id"], OperatorNotice(message="Other project private notice"))
    path = f"/api/v3/assignments/{assigned['id']}/control-notices"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(host_token)).status_code == 403
    response = client.get(path, headers=auth(grant["execution_token"]))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert len(response.content) <= CONTROL_SUMMARY_BYTES
    summary = response.json()
    assert summary["pending"] == 12
    assert 0 < len(summary["notices"]) <= CONTROL_NOTICE_LIMIT
    assert summary["omitted"] == 12 - len(summary["notices"])
    assert "Other project" not in response.text
    assert client.get(f"/api/v3/assignments/{other_assigned['id']}/control-notices",
                      headers=auth(grant["execution_token"])).status_code == 403
    with database.transaction() as conn:
        world.conn = conn
        assert all(row["disposition"] == "pending" and row["delivered_at"] is None
                   for row in notices(world, assigned["id"]))
        change(conn, "execution", grant["execution_id"], lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert client.get(path, headers=auth(grant["execution_token"])).status_code == 409
    assert client.get(path, headers=auth(operator_token)).status_code == 200
