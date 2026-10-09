from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import insert

from archon_horizon.pipeline.auth import Actor, authenticate, require_project
from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ConnectorManager
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.models import ForgeComment
from archon_horizon.pipeline.persistence.records import change, create
from archon_horizon.pipeline.review.decisions import queue_comment
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_service import service_database, world  # noqa: F401


def queued(world, *, role="worker"):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run, role=role)
    claim = world.claim()
    actor = authenticate(world.conn, claim["execution_token"])
    item = create(world.conn, "forge_item", repository_id=world.workspace_repo["id"], kind="pull_request",
                  remote_number=1, title="Library statement", status="open", head_commit_oid="a" * 40,
                  observed_at=datetime.now(timezone.utc))
    operation = queue_comment(world.conn, actor, ForgeComment(forge_item_id=item["id"], body="Review intent"),
                              world.scheduler, str(uuid4()))
    return run, assignment, claim, actor, operation


@pytest.mark.parametrize("status,assignment_status", [("succeeded", "pending"), ("succeeded", "completed"),
                                                     ("failed", "failed"), ("lost", "pending")])
def test_authorized_queue_survives_finished_execution_but_api_lease_does_not(world, status, assignment_status):
    run, assignment, claim, actor, operation = queued(world)
    change(world.conn, "execution", claim["execution_id"], status=status,
           lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=60))
    change(world.conn, "assignment", assignment["id"], status=assignment_status)
    with pytest.raises(DomainError, match="Execution authority"):
        require_project(world.conn, actor, world.project["id"], "worker")
    @contextmanager
    def transaction():
        yield world.conn
    manager = ConnectorManager(SimpleNamespace(transaction=transaction), world.service, lambda ref: {})
    result = manager._operation_snapshot({**operation, "_reconcile_only": False})
    assert result["actor"].id == actor.id


@pytest.mark.parametrize("target,status", [("execution", "cancelled"), ("execution", "stopping"),
    ("assignment", "cancelled"), ("assignment", "stopping"), ("run", "cancelled")])
def test_explicit_cancellation_revokes_undelivered_intent(world, target, status):
    run, assignment, claim, actor, operation = queued(world)
    identifier = {"execution": claim["execution_id"], "assignment": assignment["id"], "run": run["id"]}[target]
    change(world.conn, target, identifier, status=status)
    with pytest.raises(ConnectorFailure, match="delivery_authority_revoked"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "worker")


def test_delivery_preserves_original_role_and_current_downgrade(world):
    run, assignment, claim, actor, operation = queued(world)
    change(world.conn, "assignment", assignment["id"], role="maintainer")
    with pytest.raises(ConnectorFailure, match="delivery_authority_scope_mismatch"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "maintainer")


def test_current_maintainer_downgrade_revokes_pending_maintainer_write(world):
    run, assignment, claim, actor, operation = queued(world, role="maintainer")
    ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "maintainer")
    change(world.conn, "assignment", assignment["id"], role="worker")
    with pytest.raises(ConnectorFailure, match="delivery_authority_scope_mismatch"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "maintainer")


def test_retained_delivery_cannot_cross_project_scope(world):
    run, assignment, claim, actor, operation = queued(world)
    other = create(world.conn, "project", slug="unrelated-" + uuid4().hex, title="Other")
    with pytest.raises(ConnectorFailure, match="delivery_intent_scope_mismatch"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, other["id"], "worker")


def test_disabled_actor_and_modified_or_cancelled_intent_cannot_deliver(world):
    run, assignment, claim, actor, operation = queued(world)
    with pytest.raises(ConnectorFailure, match="delivery_intent_scope_mismatch"):
        ConnectorManager._require_delivery_project(world.conn, {**operation, "payload": {}}, actor,
                                                   world.project["id"], "worker")
    change(world.conn, "principal", actor.id, disabled_at=datetime.now(timezone.utc))
    with pytest.raises(ConnectorFailure, match="actor_disabled"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "worker")
    change(world.conn, "principal", actor.id, disabled_at=None)
    change(world.conn, "outbox_operation", operation["id"], status="cancelled")
    with pytest.raises(ConnectorFailure, match="delivery_intent_scope_mismatch"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "worker")


def test_human_project_grant_revocation_remains_effective(world):
    principal = create(world.conn, "principal", kind="human", display_name="Contributor", username="delivery-" + uuid4().hex)
    actor = Actor(principal["id"], "human", {}, "api_key")
    grant = tables["project_grant"]
    world.conn.execute(insert(grant).values(principal_id=actor.id, project_id=world.project["id"], role="worker"))
    operation = create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=actor.id,
                       kind="forge_comment", schema_version=1, idempotency_key=str(uuid4()), payload={})
    ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "worker")
    world.conn.execute(grant.delete().where(grant.c.principal_id == actor.id))
    with pytest.raises(DomainError, match="does not grant"):
        ConnectorManager._require_delivery_project(world.conn, operation, actor, world.project["id"], "worker")
