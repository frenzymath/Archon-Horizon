from uuid import uuid4
import base64

import httpx
import pytest
from sqlalchemy import func

from archon_horizon.pipeline.connectors import ConnectorManager
from archon_horizon.pipeline.records import change, create, get
from test_pipeline_api import api, api_database, auth, mutate
from test_pipeline_forge_change_delivery import Forge


@pytest.mark.parametrize("status", ["running", "completed", "uncertain", "failed", "cancelled"])
@pytest.mark.parametrize("large", [False, True])
def test_polling_returns_live_delivery_but_post_replay_is_immutable(api, status, large):
    client, database, world, run, token, _ = api
    key = str(uuid4())
    body = {"repository_id": str(world.document["source_repository_id"]), "origin_run_id": str(run["id"]),
        "review_phase": run["phase"]["kind"], "kind": "issue", "title": "Inspect cluster",
        "body": "Scoped audit" * (2000 if large else 1)}
    submitted = mutate(client, "/api/v3/forge/create", body, token, key)
    assert submitted.status_code == 200, submitted.text
    original = submitted.json()
    with database.transaction() as conn:
        change(conn, "outbox_operation", original["id"], status=status,
            failure={"kind": "transport", "code": "test", "message": "Concrete failure"} if status == "failed" else None)
    current = client.get(f"/api/v3/operations/{key}?operation=forge_create", headers=auth(token))
    assert current.status_code == 200, current.text
    assert current.json()["status"] == status
    assert current.json()["revision"] > original["revision"]
    assert "no-store" in current.headers["cache-control"]
    if status == "failed":
        assert current.json()["failure"]["message"] == "Concrete failure"
    assert mutate(client, "/api/v3/forge/create", body, token, key).json() == original
    with database.transaction() as conn:
        change(conn, "outbox_operation", original["id"], status="cancelled")


def test_completed_file_delivery_exposes_verified_commit_without_file_readbacks(api):
    client, database, world, run, token, _ = api
    with database.transaction() as conn:
        source = get(conn, "repository", world.document["source_repository_id"])
        repository = create(conn, "repository", project_id=world.project["id"], slug="receipt-library",
            integration_id=source["integration_id"], remote_id="owner/library", remote_path="owner/library",
            default_branch="main", purpose="library")
    blob = mutate(client, "/api/v3/artifacts", {"project_id": str(world.project["id"]),
        "content_base64": base64.b64encode(b"milestone").decode(), "media_type": "text/plain"}, token).json()
    key = str(uuid4())
    body = {"repository_id": str(repository["id"]), "origin_run_id": str(run["id"]),
        "base_commit_oid": Forge.base, "message": "State milestone", "files": [{"operation": "create",
        "path": "new.md", "content_artifact_id": blob["id"]}]}
    submitted = mutate(client, "/api/v3/forge/change", body, token, key).json()
    forge = Forge()
    manager = ConnectorManager(database, world.service, lambda ref: {"token": "private"},
        client_factory=lambda integration: httpx.Client(transport=httpx.MockTransport(forge.handler)))
    assert manager.dispatch_one() == "completed"
    current = client.get(f"/api/v3/operations/{key}?operation=forge_change", headers=auth(token))
    assert current.status_code == 200, current.text
    data = current.json()
    assert data["operation"]["status"] == "completed"
    receipt = data["publication_receipt"]
    assert receipt["commit_oid"] == forge.commit
    assert receipt["base_commit_oid"] == forge.base
    assert receipt["branch"] == submitted["branch"]
    assert receipt["artifact_id"]
    assert receipt["changed_paths"] == ["new.md"]
    assert receipt["verification"] == "git_tree_delta"
    assert receipt["lean_validation_included"] is False
    assert mutate(client, "/api/v3/forge/change", body, token, key).json() == submitted
    with database.transaction() as conn:
        world.conn = conn
        owner = world.assignment(run)
        create(conn, "publication", artifact_id=receipt["artifact_id"], requested_by_assignment_id=owner["id"],
            target={"kind": "git", "repository_id": str(repository["id"]), "ref_name": "refs/heads/main",
                    "expected_old_oid": None}, status="verified", verified_at=func.now())
    republished = client.get(f"/api/v3/operations/{key}?operation=forge_change", headers=auth(token)).json()
    assert republished["publication_receipt"]["branch"] == submitted["branch"]
    assert republished["publication_receipt"]["publication_id"] is None


def test_delivery_poll_cannot_follow_result_into_another_project(api):
    client, database, world, run, token, _ = api
    key = str(uuid4())
    body = {"repository_id": str(world.document["source_repository_id"]), "origin_run_id": str(run["id"]),
        "review_phase": run["phase"]["kind"], "kind": "issue", "title": "Inspect", "body": "Audit"}
    submitted = mutate(client, "/api/v3/forge/create", body, token, key).json()
    with database.transaction() as conn:
        foreign = create(conn, "project", slug="foreign-" + uuid4().hex[:10], title="Other project")
        change(conn, "outbox_operation", submitted["id"], project_id=foreign["id"])
    assert client.get(f"/api/v3/operations/{key}?operation=forge_create", headers=auth(token)).status_code == 409
