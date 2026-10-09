from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select, update

from archon_horizon.pipeline.auth import Actor, authenticate
from archon_horizon.pipeline.projects.catalog import CatalogUpdate, configure_host_harness, update_catalog
from archon_horizon.pipeline.commands import Command, execute
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.review.invocations import prepare
from archon_horizon.pipeline.review.decisions import queue_review
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


def command(world, actor, operation, row, **args):
    return execute(world.conn, actor, Command(operation=operation, target_id=row["id"],
        expected_revision=row["revision"], args=args), world.service, world.scheduler)


def test_host_harness_update_validates_configuration_without_database_metadata(world):
    result = configure_host_harness(world.conn, world.actor, world.host["id"], world.harness["id"],
        CatalogUpdate(expected_revision=world.host["revision"], changes={"max_parallel_subagents": 2}))
    assert result["max_parallel_subagents"] == 2
    assert result["host_revision"] == world.host["revision"] + 1
    with pytest.raises(DomainError) as error:
        configure_host_harness(world.conn, world.actor, world.host["id"], world.harness["id"],
            CatalogUpdate(expected_revision=world.host["revision"], changes={"execution_slots": 3}))
    assert error.value.code == "revision_conflict"


def test_node_catalog_update_rejects_cycle_and_keeps_join_set(world):
    first = create(world.conn, "node", project_id=world.project["id"], number=1, title="First",
        source_repository_id=world.workspace_repo["id"], source_path="first.md", source_commit_oid="a" * 40)
    second = create(world.conn, "node", project_id=world.project["id"], number=2, title="Second",
        source_repository_id=world.workspace_repo["id"], source_path="second.md", source_commit_oid="a" * 40)
    update_catalog(world.conn, world.actor, "node", second["id"], CatalogUpdate(expected_revision=1,
        changes={"parent_node_ids": [first["id"]]}))
    with pytest.raises(DomainError) as error:
        update_catalog(world.conn, world.actor, "node", first["id"], CatalogUpdate(expected_revision=1,
            changes={"parent_node_ids": [second["id"]]}))
    assert error.value.code == "dependency_cycle"
    link = tables["node_dependency"]
    assert list(world.conn.execute(select(link.c.child_node_id, link.c.parent_node_id))) == [(second["id"], first["id"])]


def test_delivery_reauthorization_requires_owner_or_maintainer_and_refuses_uncertainty(world):
    run = world.run()
    world.disable_automations(run)
    world.assignment(run)
    world.assignment(run)
    first, second = world.claim(), world.claim()
    first_actor, second_actor = authenticate(world.conn, first["execution_token"]), authenticate(world.conn, second["execution_token"])
    delivery = create(world.conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=first_actor.id,
        kind="forge_comment", idempotency_key="old-attempt", payload={"forge_item_id": str(uuid4()), "body": "Review note"},
        schema_version=1, status="failed")
    with pytest.raises(DomainError) as error:
        command(world, second_actor, "retry_delivery", delivery, note="Retry it")
    assert error.value.code == "forbidden"
    retried = command(world, world.actor, "retry_delivery", delivery, note="Operator adopts the delivery")
    assert retried["status"] == "pending" and retried["actor_principal_id"] == world.actor.id
    uncertain = change(world.conn, "outbox_operation", delivery["id"], status="uncertain")
    with pytest.raises(DomainError) as error:
        command(world, world.actor, "retry_delivery", uncertain, note="Try again")
    assert error.value.code == "delivery_unsettled"
    with pytest.raises(DomainError):
        command(world, world.actor, "cancel_delivery", uncertain, note="Forget it")
    settled = change(world.conn, "outbox_operation", delivery["id"], status="failed")
    cancelled = command(world, world.actor, "cancel_delivery", settled, note="The message is no longer necessary")
    assert cancelled["status"] == "cancelled"


def test_physical_provider_failure_updates_shared_circuit_before_releasing_claims(world, review):
    result = handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(),
        execution_id=review["execution"]["id"], epoch=review["execution"]["number"], kind="execution_finished",
        payload={"status": "failed", "provider_thread_id": "saved-context", "failure": {
            "kind": "provider", "code": "rate_limited", "message": "Provider returned rate limit"}},
        occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)
    assert result["status"] == "pending"
    limit = get(world.conn, "resource_limit", review["limit"]["id"])
    assert limit["failure_count"] == 1 and limit["cooldown_until"] is not None
    assert not world.conn.execute(select(tables["resource_claim"].c.id).where(
        tables["resource_claim"].c.execution_id == review["execution"]["id"], tables["resource_claim"].c.released_at.is_(None))).first()


def test_lost_execution_preserves_provider_claim_until_physical_stop(world, review):
    change(world.conn, "execution", review["execution"]["id"], lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    world.scheduler.tick(world.conn)
    assert get(world.conn, "provider_request", review["request"]["id"])["status"] == "uncertain"
    claims = tables["resource_claim"]
    assert world.conn.execute(select(claims.c.id).where(claims.c.execution_id == review["execution"]["id"],
        claims.c.released_at.is_(None))).first()
    thread = get(world.conn, "provider_thread", review["thread"]["id"])
    with pytest.raises(DomainError) as error:
        command(world, world.actor, "recover_context", thread, note="Previous context cannot be resumed")
    assert error.value.code == "request_unsettled"
    handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(), execution_id=review["execution"]["id"],
        epoch=review["execution"]["number"], kind="execution_finished", payload={"status": "lost", "reason": "process stopped"},
        occurred_at=datetime.now(timezone.utc).timestamp()), world.service, world.scheduler)
    assert not world.conn.execute(select(claims.c.id).where(claims.c.execution_id == review["execution"]["id"],
        claims.c.released_at.is_(None))).first()
    old = get(world.conn, "provider_thread", thread["id"])
    before = world.ledger(review["assignment"]["id"])
    replacement = command(world, world.actor, "recover_context", old, note="Explicitly rebuild missing native history")
    assert replacement["predecessor_id"] == old["id"] and replacement["workspace_id"] == old["workspace_id"]
    assert replacement["skill_bundle_artifact_id"] == old["skill_bundle_artifact_id"]
    assert replacement["provider_thread_id"] is None
    assert world.ledger(review["assignment"]["id"]) == before


def test_failed_unowned_assignment_has_explicit_run_blocker(world):
    run = world.run()
    world.disable_automations(run)
    assignment = world.assignment(run)
    grant = world.claim()
    world.scheduler.finish(world.conn, world.host_actor, get(world.conn, "execution", grant["execution_id"]), "failed",
        {"kind": "execution", "code": "irrecoverable", "message": "Reconsider the plan"})
    assert any(item["kind"] == "blocker" and "no active planner owner" in item["description"]
               for item in world.ledger(assignment["id"]))


def test_project_viewer_cannot_read_another_projects_context(world, review):
    principal = create(world.conn, "principal", kind="service", service_name="other-project-reader", display_name="Reader")
    project = create(world.conn, "project", slug="other-project", title="Unrelated")
    world.conn.execute(insert(tables["project_grant"]).values(principal_id=principal["id"], project_id=project["id"], role="viewer"))
    actor = Actor(principal["id"], "service", {"service_name": "other-project-reader"}, "api_key")
    with pytest.raises(DomainError) as error:
        world.service.context(world.conn, actor, review["assignment"]["id"])
    assert error.value.code == "forbidden"


def test_published_review_uses_invocation_revision_and_rejects_unreviewed_head(world, review):
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    request = change(world.conn, "provider_request", result["provider_request_id"], status="completed")
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], instructions="A later, stricter rubric")
    payload = {"forge_item_id": review["item"]["id"], "commit_oid": "a" * 40, "verdict": "commented", "summary": "Aligned",
               "reviewer_descriptor_id": review["descriptor"]["id"], "provider_request_id": request["id"]}
    queued = queue_review(world.conn, review["actor"], world.service, world.scheduler, payload, "publish-review")
    assert queued["payload"]["reviewer_descriptor_revision_id"] == str(request["reviewer_descriptor_revision_id"])
    assert "### Horizon Review:" in queued["payload"]["summary"]
    assert f"Reviewer: `{review['descriptor']['slug']}`" in queued["payload"]["summary"]
    assert f"Rubric revision: {review['descriptor']['revision']}" in queued["payload"]["summary"]
    assert "Reviewer result" in queued["payload"]["summary"]
    change(world.conn, "forge_item", review["item"]["id"], target_branch="different-base")
    with pytest.raises(DomainError) as error:
        queue_review(world.conn, review["actor"], world.service, world.scheduler, payload, "wrong-new-base")
    assert error.value.code == "review_base_changed"
    historical = queue_review(world.conn, review["actor"], world.service, world.scheduler,
        {**payload, "historical": True}, "old-base-feedback")
    assert historical["payload"]["historical"]
    assert "does not approve the current PR" in historical["payload"]["summary"]
    change(world.conn, "forge_item", review["item"]["id"], head_commit_oid="b" * 40)
    with pytest.raises(DomainError):
        queue_review(world.conn, review["actor"], world.service, world.scheduler,
            {**payload, "commit_oid": "b" * 40}, "wrong-new-head")


@pytest.mark.parametrize("status", ["submitted", "interrupted", "completed"])
def test_historical_feedback_preserves_real_invocation_without_approving_or_completing_it(world, review, status):
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    change(world.conn, "provider_request", result["provider_request_id"], status=status)
    change(world.conn, "forge_item", review["item"]["id"], status="merged", head_commit_oid="b" * 40)
    payload = {"forge_item_id": review["item"]["id"], "commit_oid": "a" * 40,
        "verdict": "commented", "summary": "Actual earlier findings", "historical": True,
        "reviewer_descriptor_id": review["descriptor"]["id"], "provider_request_id": result["provider_request_id"]}
    operation = queue_review(world.conn, review["actor"], world.service, world.scheduler, payload, "historical-" + status)
    assert f"`{status}`" in operation["payload"]["summary"]
    assert get(world.conn, "provider_request", result["provider_request_id"])["status"] == status
    with pytest.raises(DomainError) as error:
        queue_review(world.conn, review["actor"], world.service, world.scheduler,
            {**payload, "commit_oid": "b" * 40}, "misattributed")
    assert error.value.code == "review_head_changed"


def test_historical_feedback_uses_configured_publisher_but_approval_keeps_pinned_identity(world, review):
    repository = get(world.conn, "repository", review["item"]["repository_id"])
    def identity(name):
        return create(world.conn, "integration_identity", integration_id=repository["integration_id"],
            remote_user_id=name, principal_id=world.actor.id, credential_ref="secret:" + name)
    original, specialist = identity("maintainer"), identity("specialist")
    change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=original["id"])
    result = prepare(world.conn, review["actor"], review["data"], world.service)
    change(world.conn, "provider_request", result["provider_request_id"], status="completed")
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], integration_identity_id=specialist["id"])
    payload = {"forge_item_id": review["item"]["id"], "commit_oid": "a" * 40,
        "verdict": "approved", "summary": "Scoped findings", "reviewer_descriptor_id": review["descriptor"]["id"],
        "provider_request_id": result["provider_request_id"]}
    current = queue_review(world.conn, review["actor"], world.service, world.scheduler, payload, "pinned-identity")
    old = queue_review(world.conn, review["actor"], world.service, world.scheduler,
        {**payload, "verdict": "commented", "historical": True}, "historical-identity")
    assert current["payload"]["integration_identity_id"] == str(original["id"])
    assert old["payload"]["integration_identity_id"] == str(specialist["id"])
    assert old["payload"]["reviewer_descriptor_revision_id"] == current["payload"]["reviewer_descriptor_revision_id"]


@pytest.mark.parametrize("identity_state", ["enabled", "disabled", "other_integration"])
def test_descriptor_without_account_uses_only_valid_policy_identity(world, review, identity_state):
    repository = get(world.conn, "repository", review["item"]["repository_id"])
    integration_id = repository["integration_id"]
    if identity_state == "other_integration":
        integration_id = create(world.conn, "integration", kind="forge", endpoint="https://other.invalid",
                                credential_ref="secret:other")["id"]
    identity = create(world.conn, "integration_identity", integration_id=integration_id,
                      remote_user_id="maintainer", principal_id=world.actor.id,
                      credential_ref="secret:maintainer", enabled=identity_state != "disabled")
    change(world.conn, "review_policy", world.policy["id"], maintainer_identity_id=identity["id"])
    payload = {"forge_item_id": review["item"]["id"], "commit_oid": "a" * 40,
               "verdict": "commented", "summary": "Scoped assessment",
               "reviewer_descriptor_id": review["descriptor"]["id"]}
    if identity_state == "enabled":
        queued = queue_review(world.conn, review["actor"], world.service, world.scheduler, payload, "policy-identity")
        assert queued["payload"]["integration_identity_id"] == str(identity["id"])
    else:
        with pytest.raises(DomainError) as error:
            queue_review(world.conn, review["actor"], world.service, world.scheduler, payload, "policy-identity")
        assert error.value.code == "maintainer_identity_unavailable"
