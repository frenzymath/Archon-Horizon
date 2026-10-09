import base64
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, update

from archon_horizon.pipeline.auth import issue_credential
from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ForgejoClient
from archon_horizon.pipeline.persistence.records import create
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api, api_database, auth


def test_exact_file_and_paginated_review_inspection_reject_head_race():
    calls = []
    head = "a" * 40

    def handler(request):
        nonlocal head
        calls.append(request)
        if "/contents/" in request.url.path:
            assert request.url.params["ref"] == "a" * 40
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "sha": "b" * 40,
                "content": base64.b64encode(b"theorem target : True := True.intro\n").decode()})
        if request.url.path.endswith(".diff"):
            return httpx.Response(200, text="line1\nline2\nline3\n")
        if request.url.path.endswith("/files"):
            assert request.url.params["page"] == "2"
            head = "c" * 40
            return httpx.Response(200, json=[{"filename": "Proof.lean"}])
        if request.url.path.endswith("/comments"):
            assert request.url.params["page"] == "2"
            return httpx.Response(200, json=[{"id": 6, "body": "Check hypotheses"}])
        return httpx.Response(200, json={"head": {"sha": head}})

    remote = ForgejoClient("https://forge.invalid", "private", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert remote.file_at_commit("owner/repo", head, "Proof.lean")["text"].startswith("theorem")
    for path in ("../escape", "/absolute", "a//b"):
        with pytest.raises(ConnectorFailure):
            remote.file_at_commit("owner/repo", head, path)
    comments = remote.inspect_item("owner/repo", 1, kind="pull_request", view="review_comments",
                                  expected_head_oid=head, page=2, limit=1, review_id=5)
    assert comments["next_page"] == 3 and comments["items"][0]["id"] == 6
    diff = remote.inspect_item("owner/repo", 1, kind="pull_request", view="diff", expected_head_oid=head, page=2, limit=2)
    assert diff["diff"] == "line3\n" and not diff["has_more"]
    with pytest.raises(ConnectorFailure, match="review_head_changed"):
        remote.inspect_item("owner/repo", 1, kind="pull_request", view="files", expected_head_oid=head, page=2, limit=1)
    assert all(request.method == "GET" for request in calls)


def test_inspection_api_scopes_credentials_and_rechecks_current_auth(api, monkeypatch):
    client, database, world, run, operator_token, _ = api
    from archon_horizon.pipeline.integrations import forge_inspection

    calls = []
    revoke = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "token scoped-forge-secret"
        if revoke:
            with database.transaction() as conn:
                conn.execute(update(tables["credential"]).where(tables["credential"].c.principal_id == revoke[0]).values(
                    revoked_at=datetime.now(timezone.utc)))
        if "/contents/" in request.url.path:
            return httpx.Response(200, json={"type": "file", "encoding": "base64", "sha": "b" * 40,
                                           "content": base64.b64encode(b"exact source").decode()})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json=[{"filename": "Proof.lean"}])
        return httpx.Response(200, json={"head": {"sha": "a" * 40}})

    monkeypatch.setattr(forge_inspection, "SecretResolver", lambda root: lambda reference: {"token": "scoped-forge-secret"})
    monkeypatch.setattr(forge_inspection, "ForgejoClient", lambda endpoint, token: ForgejoClient(endpoint, token,
        client=httpx.Client(transport=httpx.MockTransport(handler))))
    with database.transaction() as conn:
        conn.execute(update(tables["repository"]).where(tables["repository"].c.id == world.workspace_repo["id"]).values(remote_path="owner/repo"))
        item = create(conn, "forge_item", repository_id=world.workspace_repo["id"], remote_number=1,
                      kind="pull_request", title="Proof", status="open", head_commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
        human = create(conn, "principal", kind="human", display_name="Reader", username="reader_" + uuid4().hex)
        conn.execute(insert(tables["project_grant"]).values(principal_id=human["id"], project_id=world.project["id"], role="viewer"))
        _, reader_token = issue_credential(conn, human["id"], "api_key", "Scoped reader")
        other = create(conn, "project", slug="other_" + uuid4().hex, title="Other")
        unrelated = create(conn, "repository", project_id=other["id"], integration_id=world.workspace_repo["integration_id"],
                           slug="other", default_branch="main", purpose="workspace", remote_id="other", remote_path="other/private")
    response = client.get(f"/api/v3/repositories/{world.workspace_repo['id']}/file",
                          params={"commit_oid": "a" * 40, "path": "Proof.lean"}, headers=auth(reader_token))
    assert response.status_code == 200 and response.json()["text"] == "exact source", response.text
    assert "scoped-forge-secret" not in response.text and response.headers["cache-control"] == "private, no-store"
    inspected = client.get(f"/api/v3/forge-items/{item['id']}/inspect", params={"view": "files", "expected_head_oid": "a" * 40}, headers=auth(reader_token))
    assert inspected.status_code == 200 and inspected.json()["items"][0]["filename"] == "Proof.lean"
    count = len(calls)
    denied = client.get(f"/api/v3/repositories/{unrelated['id']}/file", params={"commit_oid": "a" * 40, "path": "Private.lean"}, headers=auth(reader_token))
    assert denied.status_code == 403 and len(calls) == count
    revoke.append(human["id"])
    revoked = client.get(f"/api/v3/repositories/{world.workspace_repo['id']}/file", params={"commit_oid": "a" * 40, "path": "Proof.lean"}, headers=auth(reader_token))
    assert revoked.status_code == 401, revoked.text
