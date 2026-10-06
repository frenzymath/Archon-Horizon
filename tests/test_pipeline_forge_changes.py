from __future__ import annotations

import base64
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from archon_horizon.pipeline.records import create
from test_pipeline_api import api, api_database, mutate


def test_pr_defaults_to_main_and_nondefault_base_requires_stack_explanation(api):
    client, _, world, run, token, _ = api
    payload = {"repository_id": str(world.document["source_repository_id"]), "origin_run_id":str(run["id"]),
        "review_phase":"preprocessing", "kind":"pull_request", "title":"Statement", "body":"Evidence", "head":"proposal"}
    response = mutate(client, "/api/v3/forge/create", payload, token)
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["base"] == "main"
    assert mutate(client, "/api/v3/forge/create", {**payload,"base":"another-proposal"},token).status_code == 422
    stacked = mutate(client, "/api/v3/forge/create", {**payload,"base":"another-proposal",
        "stack_reason":"Depends on PR1; retarget to main after its merge."},token)
    assert stacked.status_code == 200
    assert "Stack dependency:" in stacked.json()["payload"]["body"]


def test_existing_pr_amendment_checks_head_and_repository(api):
    client, database, world, run, token, _ = api
    repo = world.document["source_repository_id"]
    with database.transaction() as conn:
        item = create(conn,"forge_item",repository_id=repo,remote_number=1,kind="pull_request",
            title="Proof",status="open",head_commit_oid="a"*40,target_branch="main",observed_at=datetime.now(timezone.utc))
    artifact = mutate(client,"/api/v3/artifacts",{"project_id":str(world.project["id"]),
        "content_base64":base64.b64encode(b"fixed").decode(),"media_type":"text/plain"},token).json()
    payload = {"repository_id":str(repo),"origin_run_id":str(run["id"]),"forge_item_id":str(item["id"]),
        "base_commit_oid":"a"*40,"message":"Small maintainer repair","files":[
            {"operation":"create","path":"fixed.md","content_artifact_id":artifact["id"]}]}
    good = mutate(client,"/api/v3/forge/change",payload,token)
    assert good.status_code == 200, good.text
    assert good.json()["branch"] is None
    assert mutate(client,"/api/v3/forge/change",{**payload,"base_commit_oid":"b"*40},token).status_code == 409


def test_file_proposal_is_durable_and_never_selects_a_protected_branch(api):
    client, _, world, run, token, _ = api
    artifact = mutate(client, "/api/v3/artifacts", {"project_id": str(world.project["id"]),
        "content_base64": base64.b64encode(b"# A reviewed milestone\n").decode(), "media_type": "text/plain"}, token).json()
    payload = {"repository_id": str(world.document["source_repository_id"]), "origin_run_id": str(run["id"]),
        "base_commit_oid": "a" * 40, "message": "State the milestone",
        "files": [{"operation": "create", "path": "milestones/convexity.md", "content_artifact_id": artifact["id"]}]}
    key = str(uuid4())
    result = mutate(client, "/api/v3/forge/change", payload, token, key)
    assert result.status_code == 200, result.text
    operation = result.json()["operation"]
    assert operation["kind"] == "forge_change" and operation["status"] == "pending"
    assert result.json()["branch"] == "horizon/changes/" + operation["id"]
    assert mutate(client, "/api/v3/forge/change", payload, token, key).json() == result.json()
    assert mutate(client, "/api/v3/forge/change", {**payload, "branch": "main"}, token).status_code == 422
    assert mutate(client, "/api/v3/forge/change", {**payload, "base_commit_oid": "main"}, token).status_code == 422


@pytest.mark.parametrize("invalid", ["project", "oversized", "not_blob"])
def test_file_proposal_rejects_unusable_artifact(api, invalid):
    client, database, world, run, token, _ = api
    with database.transaction() as conn:
        project_id = world.project["id"]
        if invalid == "project":
            project_id = create(conn, "project", number=90000, slug="other-" + uuid4().hex, title="Other")["id"]
        artifact = create(conn, "artifact", project_id=project_id,
            kind="commit" if invalid == "not_blob" else "blob",
            content={"repository_id": str(world.workspace_repo["id"]), "commit_oid": "a" * 40} if invalid == "not_blob" else
                {"sha256": "b" * 64, "size_bytes": 1024**2 + 1 if invalid == "oversized" else 1, "media_type": "text/plain"})
    payload = {"repository_id": str(world.document["source_repository_id"]), "origin_run_id": str(run["id"]),
        "base_commit_oid": "a" * 40, "message": "Propose change",
        "files": [{"operation": "create", "path": "statement.md", "content_artifact_id": str(artifact["id"])}]}
    response = mutate(client, "/api/v3/forge/change", payload, token)
    assert response.status_code == 422, response.text
