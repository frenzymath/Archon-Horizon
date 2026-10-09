from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ForgejoClient
from archon_horizon.pipeline.models import ForgeCreate
from archon_horizon.pipeline.persistence.records import create, get
from test_pipeline_connectors import connector_world, manager


@pytest.mark.parametrize("field", ["head", "base"])
def test_pr_contract_rejects_commit_instead_of_branch(field):
    data = dict(repository_id=uuid4(), origin_run_id=uuid4(), review_phase="postprocessing",
                kind="pull_request", title="Proof", body="Evidence", head="proof", base="main")
    data[field] = "a" * 40
    with pytest.raises(ValidationError, match="branch names, not commit hashes"):
        ForgeCreate(**data)


@pytest.mark.parametrize("published", [False, True])
def test_old_commit_head_request_reconciles_marker_before_rejecting(published):
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(200, json=[{"number": 7, "user": {"id": 1},
            "body": "<!-- horizon-operation:old -->"}] if published else [])
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ForgejoClient("https://forge.invalid", "secret", client=transport)
        args = dict(kind="pull_request", title="Proof", body="Evidence", head="a" * 40,
                    base="main", operation_id="old", reconcile_only=True)
        if published:
            assert client.create_item("owner/repo", **args)["number"] == 7
        else:
            with pytest.raises(ConnectorFailure, match="requires_branch_names") as failure:
                client.create_item("owner/repo", **args)
            assert not failure.value.uncertain


@pytest.mark.parametrize("kind,head,blocked", [
    ("forge_change", None, False), ("forge_create", "independent-proof", False),
    ("forge_create", "same-proof", True), ("forge_create", None, False),
])
def test_uncertain_pr_blocks_only_its_own_branch(connector_world, kind, head, blocked):
    world = connector_world
    with world.database.transaction() as conn:
        common = dict(project_id=world.project["id"], actor_principal_id=world.actor["id"], schema_version=1)
        repository = str(world.repository["id"])
        create(conn, "outbox_operation", **common, kind="forge_create", idempotency_key="uncertain-pr",
               status="uncertain", retry_at=datetime.now(timezone.utc) + timedelta(hours=1),
               created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
               payload={"repository_id": repository, "kind": "pull_request", "head": "same-proof"})
        payload = {"repository_id": repository}
        if kind == "forge_create":
            payload.update(kind="pull_request" if head else "issue", head=head)
        pending = create(conn, "outbox_operation", **common, kind=kind, idempotency_key="independent",
                         payload=payload)
    claimed = manager(world, lambda request: httpx.Response(200, json={}))._claim_operation()
    assert (claimed is None) if blocked else claimed["id"] == pending["id"]


def test_invalid_legacy_pr_does_not_guess_that_uncertain_write_had_no_effect(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        mission = create(conn, "mission", project_id=world.project["id"], number=1, title="Proof", objective="Prove theorem")
        run = create(conn, "run", mission_id=mission["id"], phase={"kind": "postprocessing"}, retry_policy={})
        operation = create(conn, "outbox_operation", project_id=world.project["id"],
            actor_principal_id=world.actor["id"], kind="forge_create", idempotency_key="old-pr",
            schema_version=1, status="uncertain", payload={"repository_id": str(world.repository["id"]),
            "origin_run_id": str(run["id"]), "review_phase": "postprocessing", "kind": "pull_request",
            "title": "Proof", "body": "Evidence", "head": "a" * 40, "base": "main"})
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"id": 1} if request.url.path.endswith("/user") else [])
    assert manager(world, handler).dispatch_one() == "uncertain"
    with world.database.transaction() as conn:
        result = get(conn, "outbox_operation", operation["id"])
        assert result["failure"]["message"] == "pull_request_requires_branch_names_not_commit_hashes"


def test_delivery_receipt_keeps_actionable_local_validation_message(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        operation = create(conn, "outbox_operation", project_id=world.project["id"],
            actor_principal_id=world.actor["id"], kind="forge_change", idempotency_key="invalid-files",
            schema_version=1, payload={"repository_id": str(world.repository["id"])})
    delivery = manager(world, lambda request: pytest.fail("Validation must not send a request"))
    claimed = delivery._claim_operation()
    failure = ConnectorFailure("change_path_exists", message="Existing.lean already exists at the selected base")
    assert delivery._settle_operation(claimed, {}, None, failure) == "failed"
    with world.database.transaction() as conn:
        result = get(conn, "outbox_operation", operation["id"])
        assert result["failure"]["message"] == "change_path_exists: Existing.lean already exists at the selected base"
