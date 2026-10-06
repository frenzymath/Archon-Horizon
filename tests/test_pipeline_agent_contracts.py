import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, update

from archon_horizon.pipeline.auth import issue_credential
from archon_horizon.pipeline import models
from archon_horizon.pipeline.client import AgentClient
from archon_horizon.pipeline.command_args import COMMAND_ARGS
from archon_horizon.pipeline.commands import COMMAND_TARGETS
from archon_horizon.pipeline.connectors import ForgejoClient
from archon_horizon.pipeline.records import create, get
from archon_horizon.pipeline.schema import tables
from test_pipeline_api import api, api_database, auth, mutate


def test_repository_listing_is_scoped_for_non_admin_and_rejects_ignored_filters(api):
    client, database, world, _, _, _ = api
    with database.transaction() as conn:
        reader = create(conn, "principal", kind="human", username="reader_" + uuid4().hex, display_name="Reader")
        conn.execute(insert(tables["project_grant"]).values(principal_id=reader["id"], project_id=world.project["id"], role="viewer"))
        _, token = issue_credential(conn, reader["id"], "api_key", "Repository reader")
        wanted = create(conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=1,
                        kind="pull_request", title="Fresh library", status="open", observed_at=datetime.now(timezone.utc))
        create(conn, "forge_item", repository_id=world.document["source_repository_id"], remote_number=1,
               kind="pull_request", title="Old repository", status="open", observed_at=datetime.now(timezone.utc))
    route = "/api/v3/records/forge_item"
    for extra in ({}, {"project_id": str(world.project["id"])}):
        response = client.get(route, params={"repository_id": str(world.workspace_repo["id"]), **extra}, headers=auth(token))
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["items"]] == [str(wanted["id"])]
    for route, params in (
        (route, {"repository": str(world.workspace_repo["id"])}),
        ("/api/v3/forge-items", {"project_id": str(world.project["id"]), "repository_id": str(world.workspace_repo["id"])}),
        ("/api/v3/records/mission", {"repository_id": str(world.workspace_repo["id"])}),
        (route, [("repository_id", str(world.workspace_repo["id"])), ("repository_id", str(world.workspace_repo["id"]))]),
    ):
        response = client.get(route, params=params, headers=auth(token))
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "invalid_query"


def test_filters_follow_ancestors_and_enforce_project_permissions(api):
    client, database, world, run, token, _ = api
    with database.transaction() as conn:
        world.conn = conn
        other_run = world.run()
        world.assignment(run)
        world.assignment(other_run)
    response = client.get("/api/v3/records/assignment", params={"run_id": str(run["id"])}, headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json()["items"] and all(row["run_id"] == str(run["id"]) for row in response.json()["items"])
    response = client.get("/api/v3/records/assignment", params={"mission_id": str(world.mission["id"]), "status": "pending"}, headers=auth(token))
    assert response.status_code == 200, response.text
    assert {row["run_id"] for row in response.json()["items"]} == {str(run["id"]), str(other_run["id"])}


def test_narrow_schema_discovers_queries_commands_and_scopes(api):
    client, _, _, _, token, _ = api
    assert set(COMMAND_ARGS) == set(COMMAND_TARGETS)
    def schema(section, name):
        response = client.get("/api/v3/schema", params={"section": section, "name": name}, headers=auth(token))
        assert response.status_code == 200, response.text
        return response.json()
    query = schema("queries", "GET /api/v3/forge-items/{identifier}/inspect")
    fields = {item["name"]: item for item in query["parameters"]}
    assert fields["view"]["schema"]["enum"] == ["files", "diff", "comments", "reviews", "review_comments"]
    assert fields["limit"]["schema"]["maximum"] == 100
    command = schema("command_args", "move_before")
    assert command["required"] == ["other_id"] and command["additionalProperties"] is False
    assert "repository_id" in schema("record_filters", "forge_item")
    assert "repository_id" not in schema("record_filters", "mission")
    assert client.get("/api/v3/schema", params={"section": "nonexistent"}, headers=auth(token)).status_code == 422


@pytest.mark.parametrize("section,name,correct_section,correct_name", [
    ("command_args", "prepare_reviewer", "operations", "prepare_reviewer"),
    ("command_args", "forge_change", "operations", "forge_change"),
    ("create", "POST /api/v3/reviewer-invocations", "operations", "prepare_reviewer"),
    ("create", "POST /api/v3/forge/change", "operations", "forge_change"),
    ("create", "POST /api/v3/forge/create", "operations", "forge_create"),
    ("create", "PATCH /api/v3/missions/{id}", "mission_update", None),
    ("create", "POST /api/v3/records/{kind}", "create", None),
    ("operations", "POST /api/v3/commands", "command", None),
    ("operations", "commands", "commands", None),
    ("operations", "defer_automation", "command_args", "defer_automation"),
    ("command_args", "mission", "create", "mission"),
    ("create", "GET /api/v3/forge-items/{identifier}/inspect", "queries", "GET /api/v3/forge-items/{identifier}/inspect"),
    ("operations", "GET /api/v3/documents/{id}/milestones", "queries", "GET /api/v3/documents/{identifier}/milestones"),
    ("create", "GET /api/v3/records/document/{id}", "queries", "GET /api/v3/records/{kind}/{identifier}"),
    ("operations", "objective_milestones", "queries", "GET /api/v3/documents/{identifier}/milestones"),
    ("operations", "unknown_operation", "operations", None),
    ("nonexistent", "prepare_reviewer", "operations", "prepare_reviewer"),
    (None, "prepare_reviewer", "operations", "prepare_reviewer"),
])
def test_schema_lookup_errors_provide_a_valid_canonical_command(api, section, name, correct_section, correct_name):
    import shlex

    client, _, _, _, token, _ = api
    params = {"name": name}
    if section is not None:
        params["section"] = section
    response = client.get("/api/v3/schema", params=params, headers=auth(token))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    correction = error["correction"]
    assert error["code"] == "invalid_query"
    assert correction["section"] == correct_section
    assert correction.get("name") == correct_name
    assert correction["command"] in error["message"]
    assert "record kinds" in error["tip"] and error["allowed"]
    expected = ["horizon-pipeline", "agent", "schema", "--section", correct_section]
    if correct_name is not None:
        expected.extend(["--name", correct_name])
    assert shlex.split(correction["command"]) == expected
    corrected = client.get("/api/v3/schema", params={key: value for key, value in correction.items()
                                                  if key in {"section", "name"}}, headers=auth(token))
    assert corrected.status_code == 200, corrected.text


@pytest.mark.parametrize("name,canonical", [
    ("GET /api/v3/documents/{id}/milestones", "GET /api/v3/documents/{identifier}/milestones"),
    ("GET /api/v3/records/document/{id}", "GET /api/v3/records/{kind}/{identifier}"),
    ("GET /api/v3/records/provider_request/{id}", "GET /api/v3/records/{kind}/{identifier}"),
    ("GET /api/v3/records/document?project_id=...", "GET /api/v3/records/{kind}"),
    ("GET /api/v3/forge-items/{id}/inspect?view=...&expected_head_oid=...", "GET /api/v3/forge-items/{identifier}/inspect"),
])
def test_query_schema_accepts_declared_templates_and_known_concrete_kinds(api, name, canonical):
    client, _, _, _, token, _ = api
    response = client.get("/api/v3/schema", params={"section": "queries", "name": name}, headers=auth(token))
    expected = client.get("/api/v3/schema", params={"section": "queries", "name": canonical}, headers=auth(token))
    assert response.status_code == 200, response.text
    assert response.json() == expected.json()


def test_every_published_get_route_is_a_discoverable_query_schema(api):
    client, _, _, _, token, _ = api
    routes = client.get("/api/v3/schema", params={"section": "routes"}, headers=auth(token)).json()
    assert routes["objective_milestones"] == "GET /api/v3/documents/{identifier}/milestones"
    assert routes["read"] == "GET /api/v3/records/{kind}/{identifier}"
    for route in routes.values():
        if route.startswith("GET "):
            response = client.get("/api/v3/schema", params={"section": "queries", "name": route}, headers=auth(token))
            assert response.status_code == 200, (route, response.text)


def test_query_schema_normalization_keeps_unknown_and_ambiguous_routes_rejected():
    from archon_horizon.pipeline.schema_discovery import canonical_query_name

    contracts = {"queries": {"GET /api/v3/records/{kind}/{identifier}": {},
                            "GET /api/v3/examples/{first}": {}, "GET /api/v3/examples/{second}": {}},
                 "record_filters": {"document": []}}
    for route in ("GET /api/v3/records/unknown/{id}", "GET /api/v3/records/document/arbitrary-text",
                  "GET /api/v3/examples/{id}", "POST /api/v3/records/document/{id}", "GET //[invalid"):
        assert canonical_query_name(contracts, route) is None


def test_commands_validate_argument_types_before_database_use(api):
    client, _, _, run, token, _ = api
    response = mutate(client, "/api/v3/commands", {"operation": "move_before", "target_id": str(uuid4()),
        "expected_revision": 1, "args": {"other_id": "not-a-uuid"}}, token)
    assert response.status_code == 422, response.text


def test_automation_creation_is_bound_to_run_tree_and_agent_authority(api):
    client, database, world, run, token, _ = api
    with database.transaction() as conn:
        world.conn = conn
        world.disable_automations(run)
        world.assignment(run, role="maintainer")
        grant = world.claim()
        root = get(conn, "mission", world.mission["id"])
        child = world.service.mission(conn, world.actor, models.MissionCreate(
            project_id=world.project["id"], parent_id=root["id"], expected_parent_revision=root["revision"],
            title="One contribution", objective="Review one bounded contribution",
            acceptance_criteria=["Exact-head findings are settled"], delegation_note="Review responsibility"))
        unrelated = world.service.mission(conn, world.actor, models.MissionCreate(
            project_id=world.project["id"], title="Unrelated", objective="A separate root"))
        other_run = world.run()
        world.disable_automations(other_run)
    path = "/api/v3/records/automation"
    payload = {"run_id": str(run["id"]), "mission_id": str(child["id"]), "name": "scoped-review"}
    created = mutate(client, path, payload, grant["execution_token"])
    assert created.status_code == 200, created.text
    rejected = mutate(client, path, {**payload, "name": "outside", "mission_id": str(unrelated["id"])}, token)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "scope_mismatch"
    rejected = mutate(client, path, {**payload, "name": "other-run", "run_id": str(other_run["id"])}, grant["execution_token"])
    assert rejected.status_code == 403, rejected.text
    assert rejected.json()["error"]["code"] == "forbidden"


def test_resolve_named_branch_then_read_pinned_file(api, monkeypatch):
    from archon_horizon.pipeline import forge_inspection
    client, database, world, _, token, _ = api
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path.endswith("/branches/work/review")
        return httpx.Response(200, json={"commit": {"id": "a" * 40}})
    monkeypatch.setattr(forge_inspection, "SecretResolver", lambda root: lambda ref: {"token": "private"})
    monkeypatch.setattr(forge_inspection, "ForgejoClient", lambda endpoint, token: ForgejoClient(endpoint, token,
        client=httpx.Client(transport=httpx.MockTransport(handler))))
    with database.transaction() as conn:
        conn.execute(update(tables["repository"]).where(tables["repository"].c.id == world.workspace_repo["id"]).values(remote_path="owner/repo"))
    route = f"/api/v3/repositories/{world.workspace_repo['id']}"
    result = client.get(route + "/head", params={"branch": "work/review"}, headers=auth(token))
    assert result.status_code == 200, result.text
    assert result.json()["branch"] == "work/review" and result.json()["commit_oid"] == "a" * 40
    assert result.headers["cache-control"] == "no-store"
    invalid = client.get(route + "/file", params={"commit_oid": "main", "path": "Foo.lean"}, headers=auth(token))
    assert invalid.status_code == 422 and len(calls) == 1


@pytest.mark.parametrize("status,body", [
    (422, {"error": {"code": "validation_failed"}}),
    (422, {"error": {"code": "invalid_arguments"}}),
    (422, {"error": {"code": "unknown_command"}}),
    (404, {"detail": "Not Found"}),
    (405, {"detail": "Method Not Allowed"}),
    (422, {"detail": [{"loc": ["query", "limit"], "type": "less_than_equal"}]}),
])
def test_invalid_requests_are_retained_as_evidence_without_forcing_continuations(tmp_path, status, body):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))) as transport:
        client = AgentClient("https://horizon.invalid", "token", "execution", tmp_path, client=transport)
        with pytest.raises(RuntimeError):
            client.request("POST", "/api/v3/commands", {"bad": "input"})
        assert client.pending() == []
        assert client.replay_pending() == {"completed": [], "blocked": [], "pending": 0}
        with client.connect() as conn:
            item = conn.execute("SELECT status, response FROM intent").fetchone()
            assert item["status"] == "invalid" and json.loads(item["response"]) == body


@pytest.mark.parametrize("status,body", [(409, {"error": {"code": "revision_conflict"}}),
    (401, {"error": {"code": "not_authenticated"}}), (503, {"error": {"code": "database_unavailable"}}),
    (422, {"error": {"code": "delivery_unsettled"}})])
def test_ambiguous_and_semantic_errors_still_require_reconciliation(tmp_path, status, body):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, json=body))) as transport:
        client = AgentClient("https://horizon.invalid", "token", "execution", tmp_path, client=transport)
        with pytest.raises(RuntimeError):
            client.request("POST", "/api/v3/commands", {"operation": "cancel_run"})
        assert len(client.pending()) == 1


def test_existing_contract_rejections_stop_blocking_but_preserve_evidence(tmp_path):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(409, json={"error": {"code": "revision_conflict"}}))) as transport:
        client = AgentClient("https://horizon.invalid", "token", "execution", tmp_path, client=transport)
        for command in ("malformed", "conflicted"):
            with pytest.raises(RuntimeError):
                client.request("POST", "/api/v3/commands", {"operation": command})
        with client.connect() as conn:
            first = conn.execute("SELECT id FROM intent ORDER BY created_at LIMIT 1").fetchone()[0]
            conn.execute("UPDATE intent SET response=? WHERE id=?", (json.dumps({"error": {"code": "validation_failed"}}), first))
        resumed = AgentClient("https://horizon.invalid", "token", "next-execution", tmp_path, client=transport)
        assert len(resumed.pending()) == 1 and resumed.pending()[0]["id"] != first
        with resumed.connect() as conn:
            assert conn.execute("SELECT status FROM intent WHERE id=?", (first,)).fetchone()[0] == "invalid"
