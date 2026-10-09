"""Public Lean verification is independent of milestone planning and host-fenced."""

from uuid import uuid4

from archon_horizon.pipeline.persistence.records import change, create
from test_pipeline_api import api, api_database, auth, mutate
from test_pipeline_milestones import report


def library_job(api):
    client, database, world, _, user_token, host_token = api
    with database.transaction() as conn:
        change(conn, "project", world.project["id"], workflow="graph")
        change(conn, "host", world.host["id"], health={"capabilities": {"lean_verification": 1}})
        repo = create(conn, "repository", project_id=world.project["id"], slug="library",
            integration_id=world.workspace_repo["integration_id"], remote_id="library",
            default_branch="main", purpose="library")
        workspace = create(conn, "workspace", project_id=world.project["id"], host_id=world.host["id"],
            repository_id=repo["id"], path="/library", branch_name="main", base_commit_oid="b" * 40, status="ready")
    data = {"workspace_id": str(workspace["id"]), "source_commit_oid": "a" * 40, "base_commit_oid": "b" * 40}
    queued = mutate(client, "/api/v3/lean/verifications", data, user_token)
    assert queued.status_code == 200, queued.text
    assert mutate(client, "/api/v3/lean/verifications", data, user_token).json()["id"] == queued.json()["id"]
    claim = {"host_id": str(world.host["id"]), "library_only": True}
    path = "/api/v3/worker/verification-jobs/claim"
    assert mutate(client, path, claim, user_token).status_code == 403
    response = mutate(client, path, claim, host_token)
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["id"] == queued.json()["id"]
    return job


def library_report(**changes):
    return report().model_copy(update={"kind": "library", "milestone_keys": [], "objective_paths": [],
        "targets": {}, "definitions": {}, "types": {}, "direct_admissions": [], **changes}).model_dump(mode="json")


def test_generic_library_lane_requires_live_host_lease_and_replays_exact_receipt(api):
    client, _, _, _, user_token, host_token = api
    job = library_job(api)
    detail = client.get("/api/v3/lean/verifications/" + job["id"], headers=auth(user_token))
    assert detail.status_code == 200 and "claim_token" not in detail.json()
    path = "/api/v3/worker/verification-jobs/" + job["id"] + "/finish"
    body = {"claim_token": job["claim_token"], "report": library_report()}
    assert mutate(client, path, body, user_token).status_code == 403
    assert mutate(client, path, {**body, "claim_token": str(uuid4())}, host_token).status_code == 409
    key = str(uuid4())
    finished = mutate(client, path, body, host_token, key)
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "completed"
    assert mutate(client, path, body, host_token, key).json() == finished.json()
    receipt = client.get("/api/v3/lean/checks/" + finished.json()["check_id"], headers=auth(user_token))
    assert receipt.status_code == 200 and receipt.json()["kind"] == "library"


def test_generic_library_lane_rejects_admitted_dependencies(api):
    client, _, _, _, _, host_token = api
    job = library_job(api)
    path = "/api/v3/worker/verification-jobs/" + job["id"] + "/finish"
    response = mutate(client, path, {"claim_token": job["claim_token"],
        "report": library_report(definitions={"Hidden.helper": ["sorryAx"]})}, host_token)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "admitted_definition"
