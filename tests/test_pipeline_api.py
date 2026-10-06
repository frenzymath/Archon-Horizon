from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import OperationalError

from archon_horizon.pipeline.api import create_app
from archon_horizon.pipeline.auth import PASSWORDS, issue_credential
from archon_horizon.pipeline.database import Database
from archon_horizon.pipeline.records import create, transaction_lock
from archon_horizon.pipeline.schema import tables

from test_pipeline_service import make_world


@pytest.fixture(scope="module")
def api_database():
    url = os.environ.get("HORIZON_PIPELINE_TEST_URL")
    if not url:
        pytest.skip("set HORIZON_PIPELINE_TEST_URL to an isolated PostgreSQL test database")
    schema = "pipeline_api_" + uuid4().hex
    database = Database(url, schema=schema)
    database.migrate()
    try:
        yield database
    finally:
        with database.transaction() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        database.close()


@pytest.fixture
def api(api_database, tmp_path):
    with api_database.transaction() as conn:
        transaction_lock(conn)
        world = make_world(conn, tmp_path)
        run = world.run()
        _, user_token = issue_credential(conn, world.actor.id, "api_key", "API tests")
        _, host_token = issue_credential(conn, world.host_actor.id, "host_key", "Host tests")
        conn.execute(insert(tables["password_identity"]).values(principal_id=world.actor.id,
            password_hash=PASSWORDS.hash("test-only-password")))
    config = world.service.config.model_copy(update={"secure_cookies": False})
    app = create_app(config, database=api_database, background=False)
    with TestClient(app, base_url=config.public_url, raise_server_exceptions=False) as client:
        yield client, api_database, world, run, user_token, host_token


def auth(token):
    return {"Authorization": "Bearer " + token}


def test_site_root_opens_dashboard_without_authentication(api):
    client, *_ = api
    for method in ("GET", "HEAD"):
        response = client.request(method, "/?token=must-not-be-forwarded", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/pipeline"
    assert client.post("/").status_code == 405


def mutate(client, path, payload, token, key=None):
    return client.post(path, json=payload, headers={**auth(token), "Idempotency-Key": key or str(uuid4())})


def test_reviewer_api_prepares_once_and_exposes_pinned_manifest(api):
    client, database, world, run, operator_token, _ = api
    with database.transaction() as conn:
        transaction_lock(conn)
        world.conn = conn
        world.disable_automations(run)
        world.assignment(run, role="maintainer")
        grant = world.claim()
        parent = create(conn, "provider_request", provider_thread_id=grant["provider_thread_record_id"],
            execution_id=grant["execution_id"], number=1, reason="assignment", status="running")
        descriptor = create(conn, "reviewer_descriptor", project_id=world.project["id"], slug="api-reviewer",
            functions=["reviewer"], instructions="Inspect the target statement")
        conn.execute(insert(tables["review_policy_reviewer"]).values(review_policy_id=world.policy["id"],
            reviewer_descriptor_id=descriptor["id"]))
        item = create(conn, "forge_item", repository_id=world.document["source_repository_id"], remote_number=1,
            kind="pull_request", origin_run_id=run["id"], review_phase="preprocessing", target_branch="main",
            title="Review", status="open", head_commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
    payload = {"parent_request_id": str(parent["id"]), "forge_item_id": str(item["id"]),
        "reviewer_descriptor_id": str(descriptor["id"]), "expected_head_oid": "a" * 40}
    assert mutate(client, "/api/v3/reviewer-invocations", payload, operator_token).status_code == 403
    key = str(uuid4())
    prepared = mutate(client, "/api/v3/reviewer-invocations", payload, grant["execution_token"], key)
    assert prepared.status_code == 200, prepared.text
    assert mutate(client, "/api/v3/reviewer-invocations", payload, grant["execution_token"], key).json() == prepared.json()
    request_id = prepared.json()["provider_request_id"]
    detail = client.get(f"/api/v3/reviewer-invocations/{request_id}", headers=auth(operator_token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["manifest"]["head_commit_oid"] == "a" * 40
    attached = mutate(client, f"/api/v3/reviewer-invocations/{request_id}/attach",
        {"native_key": "child-context", "native_invocation_id": "native-call"}, grant["execution_token"])
    assert attached.status_code == 200 and attached.json()["status"] == "submitted", attached.text
    with database.transaction() as conn:
        from archon_horizon.pipeline.records import change, get
        repository = get(conn, "repository", item["repository_id"])
        identity = create(conn, "integration_identity", integration_id=repository["integration_id"],
            principal_id=world.actor.id, remote_user_id="specialist", credential_ref="secret:review-test")
        change(conn, "reviewer_descriptor", descriptor["id"], integration_identity_id=identity["id"])
        secret = world.service.config.state_root / "secrets" / "review-test.json"
        secret.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        secret.write_text('{"token":"test-reviewer-private"}')
        secret.chmod(0o600)
    credentials_url = f"/api/v3/executions/{grant['execution_id']}/reviewer-accounts"
    accounts = client.get(credentials_url, headers=auth(grant["execution_token"]))
    assert accounts.status_code == 200, accounts.text
    assert accounts.headers["cache-control"] == "private, no-store"
    assert accounts.json()["accounts"][0]["token"] == "test-reviewer-private"
    assert client.get(credentials_url, headers=auth(operator_token)).status_code == 403
    report_url = f"/api/v3/reviewer-invocations/{request_id}/report"
    report_body = {"verdict": "approved", "summary": "Statement checked within the described scope."}
    published = mutate(client, report_url, report_body, grant["execution_token"], "review-report")
    assert published.status_code == 200, published.text
    assert published.json()["payload"]["integration_identity_id"] == str(identity["id"])
    assert "test-reviewer-private" not in published.text
    assert mutate(client, report_url, report_body, grant["execution_token"], "review-report").json() == published.json()
    cancelled = mutate(client, f"/api/v3/reviewer-invocations/{request_id}/cancel",
        {"note": "Wait for the child to stop"}, grant["execution_token"])
    assert cancelled.status_code == 200 and cancelled.json()["stop_required"], cancelled.text


def test_authentication_origin_and_schema_boundaries(api):
    client, _, world, _, user_token, _ = api
    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/api/v3/projects").status_code == 401
    who = client.get("/api/v3/auth/me", headers=auth(user_token))
    assert who.status_code == 200 and who.json()["role"] == "admin"
    invalid = mutate(client, "/api/v3/records/mission", {"project_id": str(world.project["id"]),
        "title": "Invalid", "objective": "Test", "extra": "must be rejected"}, user_token)
    assert invalid.status_code == 422
    cross_origin = client.post("/api/v3/records/project", json={"slug": "cross_origin", "title": "No"},
        headers={**auth(user_token), "Idempotency-Key": str(uuid4()), "Origin": "https://elsewhere.invalid"})
    assert cross_origin.status_code == 403
    assert cross_origin.json()["error"]["code"] == "invalid_origin"


def test_cookie_login_requires_origin_for_writes_and_logout_revokes(api):
    client, _, world, _, _, _ = api
    response = client.post("/api/v3/auth/login", json={"username": world.actor.owner["username"], "password": "test-only-password"})
    assert response.status_code == 200
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/api/v3/auth/me").status_code == 200
    assert client.post("/api/v3/auth/logout").status_code == 403
    assert client.post("/api/v3/auth/logout", headers={"Origin": "http://127.0.0.1:8788"}).status_code == 200
    assert client.get("/api/v3/auth/me").status_code == 401


def test_mutation_idempotency_survives_replay_and_rejects_changed_content(api):
    client, database, world, _, token, _ = api
    body = {"project_id": str(world.project["id"]), "title": "Independent mission", "objective": "Prove a lemma"}
    key = str(uuid4())
    first = mutate(client, "/api/v3/records/mission", body, token, key)
    assert first.status_code == 200, first.text
    replay = mutate(client, "/api/v3/records/mission", body, token, key)
    assert replay.status_code == 200 and replay.json() == first.json()
    conflict = mutate(client, "/api/v3/records/mission", {**body, "objective": "Different work"}, token, key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
    with database.transaction() as conn:
        count = conn.execute(select(func.count()).select_from(tables["mission"]).where(
            tables["mission"].c.project_id == world.project["id"], tables["mission"].c.title == body["title"])).scalar_one()
    assert count == 1


def test_installation_scoped_idempotency_uses_inline_result(api):
    client, database, _, _, token, _ = api
    key = str(uuid4())
    body = {"kind": "forge", "endpoint": "https://forge.invalid", "credential_ref": "secret:integration"}
    first = mutate(client, "/api/v3/records/integration", body, token, key)
    assert first.status_code == 200, first.text
    assert mutate(client, "/api/v3/records/integration", body, token, key).json() == first.json()
    with database.transaction() as conn:
        record = conn.execute(select(tables["api_request"]).where(tables["api_request"].c.idempotency_key == key)).mappings().one()
    assert record["response"] == first.json()
    assert record["response_artifact_id"] is None


def test_dashboard_reads_and_conditional_cache(api):
    client, _, world, run, token, _ = api
    routes = (
        "/api/v3/projects", f"/api/v3/projects/{world.project['id']}",
        f"/api/v3/runs?project_id={world.project['id']}", f"/api/v3/runs/{run['id']}",
        f"/api/v3/assignments?run_id={run['id']}", f"/api/v3/changes?project_id={world.project['id']}",
        f"/api/v3/discussions?project_id={world.project['id']}", "/api/v3/resources", "/api/v3/settings",
    )
    for route in routes:
        result = client.get(route, headers=auth(token))
        assert result.status_code == 200, (route, result.text)
        assert result.headers["cache-control"] in ("no-store", "private, no-cache")
        if result.headers["cache-control"] == "private, no-cache":
            assert "Authorization" in result.headers["vary"]
            cached = client.get(route, headers={**auth(token), "If-None-Match": result.headers["etag"]})
            assert cached.status_code == 304 and not cached.content
    roadmap = f"/api/v3/roadmap?project_id={world.project['id']}"
    first = client.get(roadmap, headers=auth(token))
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-cache"
    cached = client.get(roadmap, headers={**auth(token), "If-None-Match": first.headers["etag"]})
    assert cached.status_code == 304 and not cached.content
    unauthorized_cache = client.get(roadmap, headers={"If-None-Match": first.headers["etag"]})
    assert unauthorized_cache.status_code == 401


def test_claim_provider_roundtrip_heartbeat_and_completion(api):
    client, database, world, _, user_token, host_token = api
    claim_key = str(uuid4())
    claim_payload = {"host_id": str(world.host["id"]), "harness_ids": [str(world.harness["id"])]}
    claimed = mutate(client, "/api/v3/worker/claim", claim_payload, host_token, claim_key)
    assert claimed.status_code == 200, claimed.text
    replay = mutate(client, "/api/v3/worker/claim", claim_payload, host_token, claim_key)
    assert replay.status_code == 200
    original_grant, replayed_grant = claimed.json()["execution"], replay.json()["execution"]
    assert {key: value for key, value in replayed_grant.items() if key != "lease_seconds"} == {
        key: value for key, value in original_grant.items() if key != "lease_seconds"}
    assert 0 < replayed_grant["lease_seconds"] <= original_grant["lease_seconds"]
    conflict = mutate(client, "/api/v3/worker/claim", {**claim_payload, "harness_ids": []}, host_token, claim_key)
    assert conflict.status_code == 409
    grant = claimed.json()["execution"]
    assert grant is not None
    if grant.get("goal_artifact_id"):
        content = client.get(
            f"/api/v3/worker/executions/{grant['execution_id']}/artifacts/{grant['goal_artifact_id']}/content",
            headers=auth(host_token),
        )
        assert content.status_code == 200, content.text
        grant["goal"] = content.json()["goal"]
    request_id = str(uuid4())

    def event(details):
        operation_id = str(uuid4())
        payload = {"operation_id": operation_id,
            "execution_id": grant["execution_id"], "epoch": grant["epoch"], "kind": "provider_observed",
            "payload": {"provider_thread_record_id": grant["provider_thread_record_id"], **details},
            "occurred_at": datetime.now(timezone.utc).timestamp()}
        first = mutate(client, "/api/v3/worker/operations", payload, host_token, operation_id)
        replay = mutate(client, "/api/v3/worker/operations", payload, host_token, operation_id)
        assert replay.status_code == first.status_code and replay.json() == first.json()
        return first

    started = event({"event": "request_started", "request_id": request_id, "goal": grant["goal"]})
    assert started.status_code == 200, started.text
    completed = event({"event": "request_completed", "request_id": request_id, "status": "completed", "provider_thread_id": "native-thread-one"})
    assert completed.status_code == 200, completed.text
    heartbeat = mutate(client, f"/api/v3/worker/executions/{grant['execution_id']}/heartbeat",
        {"epoch": grant["epoch"], "provider_thread_id": "native-thread-one"}, host_token)
    assert heartbeat.status_code == 200, heartbeat.text
    assert heartbeat.json()["continue"] is True
    context = client.get(f"/api/v3/assignments/{grant['assignment_id']}/context", headers=auth(grant["execution_token"]))
    assert context.status_code == 200, context.text
    for obligation in context.json()["obligations"]:
        resolved = mutate(client, f"/api/v3/obligations/{obligation['id']}/resolve", {
            "expected_revision": obligation["revision"], "status": "done",
            "resolution": {"kind": "completed", "note": "The assigned work is complete", "evidence": []}}, grant["execution_token"])
        assert resolved.status_code == 200, resolved.text
    operation_id = str(uuid4())
    finish_payload = {"operation_id": operation_id,
        "execution_id": grant["execution_id"], "epoch": grant["epoch"], "kind": "execution_finished",
        "payload": {"status": "succeeded"}, "occurred_at": datetime.now(timezone.utc).timestamp()}
    finished = mutate(client, "/api/v3/worker/operations", finish_payload, host_token, operation_id)
    assert finished.status_code == 200, finished.text
    assert finished.json()["status"] == "completed"
    assert mutate(client, "/api/v3/worker/operations", finish_payload, host_token, operation_id).json() == finished.json()
    assert client.get(f"/api/v3/assignments/{grant['assignment_id']}/context", headers=auth(grant["execution_token"])).status_code == 401


def test_database_outage_returns_retryable_response_without_sensitive_details(api, monkeypatch):
    client, database, _, _, token, _ = api

    @contextmanager
    def unavailable():
        raise OperationalError("SELECT protected", {}, RuntimeError("sensitive diagnostic"))
        yield  # pragma: no cover

    monkeypatch.setattr(database, "transaction", unavailable)
    assert client.get("/health/live").status_code == 200
    response = client.get("/api/v3/projects", headers=auth(token))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "2"
    assert response.headers["cache-control"] == "no-store"
    assert "sensitive" not in response.text and "protected" not in response.text


def test_malformed_json_and_claim_identifiers_are_validation_errors(api):
    client, _, _, _, token, host_token = api
    malformed = client.post("/api/v3/records/project", content="{", headers={**auth(token), "Content-Type": "application/json"})
    assert malformed.status_code == 422
    bad_claim = mutate(client, "/api/v3/worker/claim", {"host_id": "invalid", "harness_ids": []}, host_token)
    assert bad_claim.status_code == 422


@pytest.mark.parametrize("state", ["expired", "stopping", "lost"])
def test_claim_replay_preserves_identity_but_never_reissues_expired_lease(api, state):
    client, database, world, _, _, host_token = api
    key = str(uuid4())
    body = {"host_id": str(world.host["id"]), "harness_ids": [str(world.harness["id"])]}
    original = mutate(client, "/api/v3/worker/claim", body, host_token, key)
    assert original.status_code == 200, original.text
    grant = original.json()["execution"]
    assert grant and grant["lease_seconds"] > 0
    with database.transaction() as conn:
        values = {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)} if state == "expired" else {"status": state}
        conn.execute(update(tables["execution"]).where(tables["execution"].c.id == grant["execution_id"]).values(**values))
    replay = mutate(client, "/api/v3/worker/claim", body, host_token, key)
    assert replay.status_code == 200, replay.text
    stale = replay.json()["execution"]
    assert stale["lease_seconds"] == 0
    assert stale["execution_id"] == grant["execution_id"]
    assert stale["execution_token"] == grant["execution_token"]
    with database.transaction() as conn:
        receipt = conn.execute(select(tables["api_request"].c.response).where(
            tables["api_request"].c.idempotency_key == key)).scalar_one()
        assert receipt["execution"]["lease_seconds"] > 0
        assert conn.execute(select(func.count()).select_from(tables["execution"]).where(
            tables["execution"].c.assignment_id == grant["assignment_id"])).scalar_one() == 1


def test_project_visibility_and_mutation_roles_are_enforced_for_non_admin(api):
    client, database, world, _, _, _ = api
    with database.transaction() as conn:
        viewer = create(conn, "principal", kind="human", username="viewer_" + uuid4().hex[:12], display_name="Viewer")
        conn.execute(insert(tables["project_grant"]).values(principal_id=viewer["id"], project_id=world.project["id"], role="viewer"))
        _, token = issue_credential(conn, viewer["id"], "api_key", "Scoped test")
        hidden = create(conn, "project", slug="hidden_" + uuid4().hex[:12], title="Another project")
    visible = client.get("/api/v3/projects", headers=auth(token))
    assert visible.status_code == 200, visible.text
    assert [row["id"] for row in visible.json()["items"]] == [str(world.project["id"])]
    assert client.get(f"/api/v3/projects/{hidden['id']}", headers=auth(token)).status_code == 403
    assert client.get("/api/v3/settings", headers=auth(token)).status_code == 403
    assert mutate(client, "/api/v3/records/mission", {"project_id": str(world.project["id"]),
        "title": "Forbidden", "objective": "Viewer cannot create work"}, token).status_code == 403


def test_command_receipt_rechecks_project_access_after_grant_revocation(api):
    client, database, world, run, _, _ = api
    with database.transaction() as conn:
        maintainer = create(conn, "principal", kind="human", username="maintainer_" + uuid4().hex[:12], display_name="Maintainer")
        conn.execute(insert(tables["project_grant"]).values(principal_id=maintainer["id"], project_id=world.project["id"], role="maintainer"))
        _, token = issue_credential(conn, maintainer["id"], "api_key", "Scoped maintainer")
    key = str(uuid4())
    body = {"operation": "pause_run", "target_id": str(run["id"]), "expected_revision": run["revision"], "args": {}}
    command = mutate(client, "/api/v3/commands", body, token, key)
    assert command.status_code == 200, command.text
    assert client.get(f"/api/v3/operations/{key}", headers=auth(token)).status_code == 200
    with database.transaction() as conn:
        conn.execute(delete(tables["project_grant"]).where(tables["project_grant"].c.principal_id == maintainer["id"]))
    assert mutate(client, "/api/v3/commands", body, token, key).status_code == 403
    receipt = client.get(f"/api/v3/operations/{key}", headers=auth(token))
    assert receipt.status_code == 403
