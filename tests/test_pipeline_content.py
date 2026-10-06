from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select

from archon_horizon.pipeline import models
from archon_horizon.pipeline.records import change, get, transaction_lock
from archon_horizon.pipeline.schema import tables

from test_pipeline_api import api, api_database, auth, mutate


def test_immutable_blob_upload_download_and_authorized_conditional_cache(api):
    client, _, world, _, token, _ = api
    content = b"Pinned source evidence\n"
    payload = {"project_id": str(world.project["id"]), "content_base64": base64.b64encode(content).decode(),
               "media_type": "text/plain"}
    first = mutate(client, "/api/v3/artifacts", payload, token)
    assert first.status_code == 200, first.text
    artifact = first.json()
    assert mutate(client, "/api/v3/artifacts", payload, token).json()["id"] == artifact["id"]
    url = f"/api/v3/artifacts/{artifact['id']}/content"
    response = client.get(url, headers=auth(token))
    assert response.content == content
    assert response.headers["x-content-sha256"] == hashlib.sha256(content).hexdigest()
    assert response.headers["content-disposition"].startswith("attachment;")
    conditional = {**auth(token), "If-None-Match": response.headers["etag"]}
    assert client.get(url, headers=conditional).status_code == 304
    assert client.get(url, headers={"If-None-Match": response.headers["etag"]}).status_code == 401
    bad = mutate(client, "/api/v3/artifacts", {**payload, "content_base64": "invalid%"}, token)
    assert bad.status_code == 422


def test_record_cache_revalidates_after_revisioned_edit(api):
    client, _, world, _, token, _ = api
    url = f"/api/v3/records/document/{world.document['id']}"
    first = client.get(url, headers=auth(token))
    assert first.status_code == 200
    etag = first.headers["etag"]
    edited = client.patch(url, json={"expected_revision": first.json()["revision"], "changes": {"title": "Updated roadmap"}},
                          headers={**auth(token), "Idempotency-Key": str(uuid4())})
    assert edited.status_code == 200, edited.text
    current = client.get(url, headers={**auth(token), "If-None-Match": etag})
    assert current.status_code == 200
    assert current.headers["etag"] != etag
    assert current.json()["title"] == "Updated roadmap"


def test_resumed_assignment_replays_previous_execution_receipt_without_duplicate_effect(api):
    client, database, world, run, _, host_token = api
    with database.transaction() as conn:
        transaction_lock(conn)
        world.conn = conn
        world.disable_automations(run)
        assignment = world.assignment(run)
    claim_body = {"host_id": str(world.host["id"]), "harness_ids": [str(world.harness["id"])]}
    first = mutate(client, "/api/v3/worker/claim", claim_body, host_token).json()["execution"]
    payload = {"assignment_id": str(assignment["id"]), "description": "Account for follow-up result"}
    key = str(uuid4())
    accepted = mutate(client, "/api/v3/records/obligation", payload, first["execution_token"], key)
    assert accepted.status_code == 200, accepted.text
    with database.transaction() as conn:
        transaction_lock(conn)
        world.scheduler.finish(conn, world.host_actor, get(conn, "execution", first["execution_id"]), "yielded")
        change(conn, "assignment", assignment["id"], not_before=datetime.now(timezone.utc) - timedelta(seconds=1))
    second = mutate(client, "/api/v3/worker/claim", claim_body, host_token).json()["execution"]
    assert second["execution_id"] != first["execution_id"]
    assert second["assignment_id"] == first["assignment_id"]
    replay = mutate(client, "/api/v3/records/obligation", payload, second["execution_token"], key)
    assert replay.status_code == 200, replay.text
    assert replay.json() == accepted.json()
    with database.transaction() as conn:
        assert conn.execute(select(func.count()).select_from(tables["obligation"]).where(
            tables["obligation"].c.assignment_id == assignment["id"])).scalar_one() == 2
    denied = mutate(client, "/api/v3/records/obligation", payload, first["execution_token"], key)
    assert denied.status_code == 401
