from datetime import datetime, timezone
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest

from sqlalchemy import insert, select, update

from archon_horizon.pipeline.auth import issue_credential
from archon_horizon.pipeline import forge_inspection
from archon_horizon.pipeline.connectors import ConnectorFailure, ForgejoClient
from archon_horizon.pipeline.records import create
from archon_horizon.pipeline.schema import tables
from test_pipeline_api import api, api_database, auth, mutate  # noqa: F401


def scoped_viewer(database, project_id):
    with database.transaction() as conn:
        viewer = create(conn, "principal", kind="human", username="viewer_" + uuid4().hex, display_name="Viewer")
        conn.execute(insert(tables["project_grant"]).values(principal_id=viewer["id"], project_id=project_id, role="viewer"))
        return issue_credential(conn, viewer["id"], "api_key", "Dashboard tests")[1]


def test_host_harness_configuration_is_admin_only_and_redacted(api):
    client, database, world, _, token, host_token = api
    viewer = scoped_viewer(database, world.project["id"])
    path = f"/api/v3/hosts/{world.host['id']}/harnesses"
    with database.transaction() as conn:
        conn.execute(update(tables["host_harness"]).where(tables["host_harness"].c.host_id == world.host["id"])
                     .values(credential_ref="secret:must-not-appear", executable_path="/private/provider", provider_home="/private/auth-home"))
    assert client.get(path).status_code == 401
    assert client.get(path, headers=auth(viewer)).status_code == 403
    assert client.get(path, headers=auth(host_token)).status_code == 403
    result = client.get(path, headers=auth(token))
    assert result.status_code == 200, result.text
    assert result.json()["host_revision"] == world.host["revision"]
    assert result.json()["items"][0]["credential_configured"] is True
    assert "must-not-appear" not in result.text and "/private" not in result.text
    assert set(result.json()["items"][0]) == {"harness_id", "execution_slots", "max_parallel_subagents", "enabled", "credential_configured"}


def test_assignment_status_filter_and_pagination(api):
    client, database, world, run, token, _ = api
    with database.transaction() as conn:
        assignment = tables["assignment"]
        ids = conn.execute(select(assignment.c.id).where(assignment.c.run_id == run["id"]).order_by(assignment.c.queue_rank)).scalars().all()
        assert len(ids) >= 2
        conn.execute(update(assignment).where(assignment.c.id == ids[0]).values(status="cancelled"))
    path = f"/api/v3/assignments?run_id={run['id']}"
    pending = client.get(path + "&status=pending&limit=1", headers=auth(token))
    assert pending.status_code == 200, pending.text
    assert len(pending.json()["items"]) == 1
    assert pending.json()["items"][0]["status"] == "pending"
    history = client.get(path + "&status=history", headers=auth(token))
    assert [row["id"] for row in history.json()["items"]] == [str(ids[0])]
    assert client.get(path + "&status=unknown", headers=auth(token)).status_code == 422


def test_activity_detail_route_retains_public_text_and_authorization(api):
    client, database, world, _, token, host_token = api
    claim = mutate(client, "/api/v3/worker/claim", {"host_id": str(world.host["id"]),
        "harness_ids": [str(world.harness["id"])]}, host_token).json()["execution"]
    goal = claim["goal"]
    if claim.get("goal_artifact_id"):
        content = client.get(f"/api/v3/worker/executions/{claim['execution_id']}/artifacts/{claim['goal_artifact_id']}/content",
                             headers=auth(host_token))
        assert content.status_code == 200, content.text
        goal = content.json()["goal"]
    request_id = str(uuid4())
    def observe(payload):
        operation_id = str(uuid4())
        return mutate(client, "/api/v3/worker/operations", {"operation_id": operation_id,
            "execution_id": claim["execution_id"], "epoch": claim["epoch"], "kind": "provider_observed",
            "occurred_at": datetime.now(timezone.utc).timestamp(),
            "payload": {"provider_thread_record_id": claim["provider_thread_record_id"], "request_id": request_id, **payload}},
            host_token, operation_id)
    started = observe({"event": "request_started", "goal": goal})
    assert started.status_code == 200, started.text
    observed = observe({"event": "native_event", "adapter": "codex", "raw": {"type": "item.completed", "item": {
        "id": "tool", "type": "command_execution", "command": "lake env lean Proof.lean",
        "aggregated_output": "Check complete. Authorization: Bearer secret-value", "exit_code": 0}}})
    assert observed.status_code == 200, observed.text
    with database.transaction() as conn:
        request = conn.execute(select(tables["provider_request"]).where(
            tables["provider_request"].c.id == request_id)).mappings().one()
        assert request["status"] == "running" and request["started_at"] is not None
    path = f"/api/v3/assignments/{claim['assignment_id']}/activity"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=auth(token))
    assert response.status_code == 200, response.text
    tool = next(row for row in response.json()["items"] if row["category"] == "tool")
    assert "lake env lean Proof.lean" in tool["title"]
    assert "Check complete" in tool["detail"]
    assert "secret-value" not in response.text
    assert tool["execution_id"] == claim["execution_id"]
    detail = client.get(f"/api/v3/assignments/{claim['assignment_id']}", headers=auth(token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["activity"][0]["detail"]


def test_project_integration_links_are_project_scoped_and_credential_free(api):
    client, database, world, _, token, _ = api
    with database.transaction() as conn:
        hidden = create(conn, "project", slug="hidden_" + uuid4().hex[:12], title="Hidden")
    viewer = scoped_viewer(database, world.project["id"])
    path = f"/api/v3/projects/{world.project['id']}/integrations"
    assert client.get(path).status_code == 401
    response = client.get(path, headers=auth(viewer))
    assert response.status_code == 200, response.text
    assert response.json()["items"]
    assert "credential" not in response.text
    assert "endpoint" not in response.text
    assert client.get(f"/api/v3/projects/{hidden['id']}/integrations", headers=auth(viewer)).status_code == 403
    assert client.get(path, headers=auth(token)).status_code == 200


@pytest.fixture
def forge_head(monkeypatch):
    state = {"calls": [], "closed": 0, "payload": {"commit": {"id": "a" * 40}}}

    class Remote:
        repository_path = staticmethod(ForgejoClient.repository_path)

        def __init__(self, endpoint, token):
            state["endpoint"], state["token"] = endpoint, token

        def request(self, method, path):
            state["calls"].append((method, path))
            if callback := state.get("during_request"):
                callback()
            if error := state.get("error"):
                raise error
            return state["payload"]

        def close(self):
            state["closed"] += 1

    monkeypatch.setattr(forge_inspection, "ForgejoClient", Remote)
    monkeypatch.setattr(forge_inspection.SecretResolver, "__call__", lambda self, reference: {"token": "private-forge-token"})
    return state


def configure_head_repository(database, world):
    with database.transaction() as conn:
        conn.execute(update(tables["repository"]).where(tables["repository"].c.id == world.workspace_repo["id"])
            .values(remote_path="geometry/workspace", default_branch="release/stable"))
    return f"/api/v3/repositories/{world.workspace_repo['id']}/head"


def test_repository_head_scopes_authentication_and_quotes_branch(api, forge_head):
    client, database, world, _, token, host_token = api
    path = configure_head_repository(database, world)
    viewer = scoped_viewer(database, world.project["id"])
    with database.transaction() as conn:
        other = create(conn, "project", slug="head_hidden_" + uuid4().hex[:12], title="Other project")
    outsider = scoped_viewer(database, other["id"])
    assert client.get(path).status_code == 401
    assert client.get(f"/api/v3/repositories/{uuid4()}/head").status_code == 401
    assert client.get(path, headers=auth(host_token)).status_code == 403
    assert client.get(path, headers=auth(outsider)).status_code == 403
    assert forge_head["calls"] == []
    for credential in (viewer, token):
        response = client.get(path, headers=auth(credential))
        assert response.status_code == 200, response.text
        assert response.json() == {"repository_id": str(world.workspace_repo["id"]),
            "branch": "release/stable", "commit_oid": "a" * 40}
        assert "private-forge-token" not in response.text
        assert "endpoint" not in response.text
        assert response.headers["cache-control"] == "no-store"
    assert forge_head["calls"] == [("GET", "/api/v1/repos/geometry/workspace/branches/release%2Fstable")] * 2
    assert forge_head["closed"] == 2


@pytest.mark.parametrize("payload", [None, [], {}, {"commit": None}, {"commit": []}, {"commit": {"id": "a" * 41}}, {"commit": {"id": "bad"}}])
def test_repository_head_invalid_remote_shapes_are_controlled_errors(api, forge_head, payload):
    client, database, world, _, token, _ = api
    path = configure_head_repository(database, world)
    forge_head["payload"] = payload
    response = client.get(path, headers=auth(token))
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "invalid_branch_head"
    assert forge_head["closed"] == 1


@pytest.mark.parametrize(("transient", "status"), [(True, 503), (False, 422)])
def test_repository_head_remote_failures_are_sanitized(api, forge_head, transient, status):
    client, database, world, _, token, _ = api
    path = configure_head_repository(database, world)
    forge_head["error"] = ConnectorFailure("http_503" if transient else "http_404", transient=transient)
    response = client.get(path, headers=auth(token))
    assert response.status_code == status
    assert response.json()["error"]["message"] == "The published repository head could not be read"
    assert "private-forge-token" not in response.text
    assert forge_head["closed"] == 1


def test_repository_head_rechecks_scope_after_network_without_holding_connection(api, forge_head):
    from sqlalchemy import delete

    client, database, world, _, _, _ = api
    path = configure_head_repository(database, world)
    viewer = scoped_viewer(database, world.project["id"])

    def revoke():
        assert database.engine.pool.checkedout() == 0
        with database.transaction() as conn:
            conn.execute(delete(tables["project_grant"]).where(tables["project_grant"].c.project_id == world.project["id"]))

    forge_head["during_request"] = revoke
    response = client.get(path, headers=auth(viewer))
    assert response.status_code == 403, response.text
    assert forge_head["closed"] == 1
    assert "commit_oid" not in response.text


def test_repository_head_disabled_integration_never_contacts_forge(api, forge_head):
    client, database, world, _, token, _ = api
    path = configure_head_repository(database, world)
    with database.transaction() as conn:
        conn.execute(update(tables["integration"]).where(tables["integration"].c.id == world.workspace_repo["integration_id"])
            .values(enabled=False))
    response = client.get(path, headers=auth(token))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "forge_unavailable"
    assert forge_head["calls"] == []


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_root_redirect_preserves_only_dashboard_navigation_and_encodes_values(api, method):
    client, *_ = api
    navigation = [("tab", "projects"), ("project", "project-id"), ("view", "node"), ("node", "a/b & c"),
        ("objective", "objective-id"), ("run", "run-id"), ("session", "session-id"), ("assignment", "assignment-id"),
        ("q", "lemma + proof"), ("mode", "graph"), ("pool", "Mathlib"), ("q", "second term")]
    sensitive = [("token", "secret-token"), ("api_key", "secret-api-key"), ("password", "secret-password"),
        ("redirect", "https://attacker.invalid"), ("next", "//attacker.invalid"), ("unknown", "drop-me")]
    response = client.request(method, "/?" + urlencode([*navigation, *sensitive]), follow_redirects=False)
    assert response.status_code == 307
    target = urlsplit(response.headers["location"])
    assert target.path == "/pipeline" and not target.netloc and not target.scheme
    assert parse_qsl(target.query) == navigation
    assert "secret" not in response.headers["location"]
    assert "attacker" not in response.headers["location"]
