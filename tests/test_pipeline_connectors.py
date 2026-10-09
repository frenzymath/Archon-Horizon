from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import insert, select, text, update

from archon_horizon.pipeline.persistence.artifacts import ArtifactStore
from archon_horizon.pipeline.config import PipelineConfig
from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ConnectorManager, ForgejoClient, SecretResolver, ZulipClient
from archon_horizon.pipeline.persistence.database import Database
from archon_horizon.pipeline.persistence.records import create, get, save_blob, snapshot, transaction_lock
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.missions.service import Service


def test_secret_resolver_restricts_paths_and_permissions(tmp_path):
    directory = tmp_path / "secrets"
    directory.mkdir()
    path = directory / "forge.json"
    path.write_text('{"token":"private-token"}')
    path.chmod(0o600)
    resolver = SecretResolver(tmp_path)
    assert resolver("forge")["token"] == "private-token"
    assert resolver("secret:forge")["token"] == "private-token"
    with pytest.raises(ConnectorFailure):
        resolver("../forge")
    path.chmod(0o644)
    with pytest.raises(ConnectorFailure, match="private"):
        resolver("forge")


@pytest.mark.parametrize("header,delay", [
    ("NaN", 0), ("Infinity", 0), ("-Infinity", 0), ("1e309", 0),
    ("unknown", 0), ("-10", 0), ("30", 30),
])
def test_remote_retry_after_cannot_poison_retry_timestamps(header, delay):
    with httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(429, headers={"Retry-After": header}))) as transport:
        client = ForgejoClient("https://forge.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure) as failure:
            client.request("GET", "/api/v1/user")
    assert failure.value.transient
    assert failure.value.retry_after == delay
    # This is the operation scheduler's consumer of the normalized hint.
    assert datetime.now(timezone.utc) + timedelta(seconds=failure.value.retry_after)


def test_zulip_uncertain_post_reconciles_marker_without_duplicate_send():
    posts = []
    stored = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"result": "success", "messages": stored, "found_newest": True})
        posts.append(request)
        body = parse_qs(request.content.decode())["content"][0]
        stored.append({"id": 100, "content": body, "sender_email": "worker@example.invalid"})
        raise httpx.ReadTimeout("response lost", request=request)

    client = ZulipClient("https://zulip.invalid", "worker@example.invalid", "secret",
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ConnectorFailure) as error:
        client.post("1", "Proof", "Question about a hypothesis", "operation-1")
    assert error.value.uncertain
    assert client.post("1", "Proof", "Question about a hypothesis", "operation-1", reconcile_only=True)["id"] == 100
    assert len(posts) == 1
    with pytest.raises(ConnectorFailure, match="uncertain"):
        client.post("1", "Proof", "Different request", "operation-2", reconcile_only=True)
    assert len(posts) == 1


def test_forge_merge_checks_current_head_status_and_sends_cas():
    requests = []
    merged = False

    def handler(request):
        nonlocal merged
        requests.append(request)
        if request.url.path.endswith("/merge"):
            assert json.loads(request.content)["head_commit_id"] == "a" * 40
            merged = True
            return httpx.Response(200, json={})
        if "/statuses/" in request.url.path:
            return httpx.Response(200, json=[{"id": 1, "context": "lake", "status": "success"}])
        if "/reviews/" in request.url.path:
            return httpx.Response(200, json={"id": 5, "state": "APPROVED", "commit_id": "a" * 40})
        return httpx.Response(200, json={"number": 1, "state": "open", "head": {"sha": "a" * 40}, "merged": merged})

    client = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ConnectorFailure, match="head_changed"):
        client.merge_checked("owner/repo", 1, expected_head="b" * 40, required_checks=["lake"])
    assert not any(request.method == "POST" for request in requests)
    with pytest.raises(ConnectorFailure, match="base_changed"):
        client.merge_checked("owner/repo", 1, expected_head="a" * 40, expected_base="main", required_checks=["lake"])
    assert not any(request.method == "POST" for request in requests)
    with pytest.raises(ConnectorFailure, match="checks"):
        client.merge_checked("owner/repo", 1, expected_head="a" * 40, required_checks=["missing-check"])
    result = client.merge_checked("owner/repo", 1, expected_head="a" * 40, required_checks=["lake"], review_remote_id="5")
    assert result["merged"]
    result = client.merge_checked("owner/repo", 1, expected_head="a" * 40, required_checks=["lake"], reconcile_only=True)
    assert result["merged"]
    assert len([request for request in requests if request.method == "POST"]) == 1


@pytest.mark.parametrize("issues,pulls", [(True, False), (False, True), (False, False), (True, True)])
def test_forge_sync_respects_explicit_repository_capabilities(issues, pulls):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        if request.url.path == "/api/v1/repos/owner/repo":
            return httpx.Response(200, json={"has_issues": issues, "has_pull_requests": pulls})
        capability = issues if request.url.path.endswith("/issues") else pulls
        return httpx.Response(200, json=[{"number": 1}]) if capability else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ForgejoClient("https://forge.invalid", "secret", client=transport)
        result = client.sync_items("owner/repo")
    assert [item["_kind"] for item in result] == (["issue"] if issues else []) + (["pull_request"] if pulls else [])
    assert len(requests) == 1 + issues + pulls


@pytest.mark.parametrize("metadata", [{}, {"has_issues": True, "has_pull_requests": True}])
def test_forge_sync_does_not_hide_missing_or_inaccessible_enabled_units(metadata):
    def handler(request):
        return httpx.Response(200, json=metadata) if request.url.path == "/api/v1/repos/owner/repo" else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client = ForgejoClient("https://forge.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure, match="http_404"):
            client.sync_items("owner/repo")


@pytest.mark.parametrize("metadata", [[], {"has_issues": "false"}, {"has_pull_requests": None}])
def test_forge_sync_rejects_malformed_capabilities(metadata):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=metadata))) as transport:
        client = ForgejoClient("https://forge.invalid", "secret", client=transport)
        with pytest.raises(ConnectorFailure, match="malformed_forge_repository"):
            client.sync_items("owner/repo")


@pytest.mark.parametrize("kind", ["issue", "pull_request", "comment"])
def test_forge_creation_lost_ack_reconciles_own_marker(kind):
    stored = []
    posts = []

    def handler(request):
        if request.url.path == "/api/v1/user":
            return httpx.Response(200, json={"id": 1})
        if request.method == "GET":
            return httpx.Response(200, json=stored)
        posts.append(request)
        value = json.loads(request.content)
        stored.append({"id": 10, "number": 2, "body": value["body"], "user": {"id": 1}})
        raise httpx.ReadTimeout("response lost", request=request)

    client = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))

    def deliver(reconcile_only=False, operation_id="create-1"):
        if kind == "comment":
            return client.comment("owner/repo", 2, body="Evidence", operation_id=operation_id, reconcile_only=reconcile_only)
        return client.create_item("owner/repo", kind=kind, title="Statement", body="Evidence", head="proof", base="main",
                                  operation_id=operation_id, reconcile_only=reconcile_only)

    with pytest.raises(ConnectorFailure) as error:
        deliver()
    assert error.value.uncertain
    assert deliver(True)["id"] == 10
    assert len(posts) == 1
    stored[0]["user"]["id"] = 2
    with pytest.raises(ConnectorFailure, match="uncertain"):
        deliver(True)
    assert len(posts) == 1


@pytest.fixture
def connector_world(tmp_path):
    url = os.environ.get("HORIZON_PIPELINE_TEST_URL")
    if not url:
        pytest.skip("set HORIZON_PIPELINE_TEST_URL to an isolated PostgreSQL database")
    schema = "pipeline_connectors_" + uuid4().hex
    database = Database(url, schema=schema)
    database.migrate()
    service = Service(ArtifactStore(tmp_path / "artifacts"), PipelineConfig(database_url=url, state_root=tmp_path))
    with database.transaction() as conn:
        transaction_lock(conn)
        actor = create(conn, "principal", kind="human", display_name="Maintainer", username="maintainer")
        conn.execute(insert(tables["system_grant"]).values(principal_id=actor["id"], permission="administer_installation"))
        project = create(conn, "project", slug="project", title="Proof project")
        forge = create(conn, "integration", kind="forge", endpoint="https://forge.invalid", credential_ref="forge")
        zulip = create(conn, "integration", kind="zulip", endpoint="https://zulip.invalid", credential_ref="zulip")
        repository = create(conn, "repository", project_id=project["id"], integration_id=forge["id"], slug="roadmap",
                            remote_id="1", remote_path="owner/repo", default_branch="main", purpose="knowledge")
        discussion = create(conn, "discussion", project_id=project["id"], integration_id=zulip["id"],
                            channel_remote_id="1", topic="Proof", observed_at=datetime.now(timezone.utc))
    world = SimpleNamespace(database=database, service=service, actor=actor, project=project, forge=forge,
                            zulip=zulip, repository=repository, discussion=discussion)
    yield world
    with database.engine.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    database.close()


def manager(world, handler):
    return ConnectorManager(world.database, world.service,
                            lambda ref: {"token": "forge-secret", "email": "worker@example.invalid", "api_key": "zulip-secret"},
                            client_factory=lambda integration: httpx.Client(transport=httpx.MockTransport(handler)))


def test_outbox_preserves_target_order_across_retry_and_concurrent_dispatch(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        transaction_lock(conn)
        item = create(conn, "forge_item", repository_id=world.repository["id"], remote_number=1, kind="issue",
                      title="Problem", status="open", observed_at=datetime.now(timezone.utc))
        first = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
                       kind="forge_label", idempotency_key="label-first", schema_version=1,
                       payload={"forge_item_id": str(item["id"]), "add": ["awaiting-review"], "remove": []},
                       retry_at=datetime.now(timezone.utc) + timedelta(minutes=5))
        second = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
                        kind="forge_label", idempotency_key="label-second", schema_version=1,
                        payload={"forge_item_id": str(item["id"]), "add": [], "remove": ["awaiting-review"]})
        # Both inserts share the DB transaction timestamp; explicitly order this fixture.
        conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == first["id"]).values(
            created_at=first["created_at"] - timedelta(seconds=1)))
    one = manager(world, lambda request: httpx.Response(200, json=[]))
    two = manager(world, lambda request: httpx.Response(200, json=[]))
    assert one._claim_operation() is None
    with world.database.transaction() as conn:
        conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == first["id"]).values(retry_at=None))
    claimed = one._claim_operation()
    assert claimed["id"] == first["id"]
    assert two._claim_operation() is None
    one._settle_operation(claimed, None, None, ConnectorFailure("superseded"))
    assert two._claim_operation()["id"] == second["id"]


def test_terminal_label_housekeeping_does_not_delay_current_review_delivery(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        transaction_lock(conn)
        old_item = create(conn, "forge_item", repository_id=world.repository["id"], remote_number=10,
                          kind="pull_request", title="Old", status="closed", head_commit_oid="a" * 40,
                          target_branch="main", observed_at=datetime.now(timezone.utc))
        current_item = create(conn, "forge_item", repository_id=world.repository["id"], remote_number=11,
                              kind="pull_request", title="Current", status="open", head_commit_oid="b" * 40,
                              target_branch="main", observed_at=datetime.now(timezone.utc))
        create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
               kind="forge_label", idempotency_key="terminal-review-label:" + str(old_item["id"]) + ":closed",
               schema_version=1, payload={"forge_item_id": str(old_item["id"]), "add": ["review/closed"], "remove": []})
        current = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
                         kind="forge_review", idempotency_key="current-review", schema_version=1,
                         payload={"forge_item_id": str(current_item["id"]), "commit_oid": "b" * 40})
        conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == current["id"])
                     .values(created_at=current["created_at"] + timedelta(minutes=1)))
    claimed = manager(world, lambda request: httpx.Response(200, json={}))._claim_operation()
    assert claimed["id"] == current["id"]


def test_forge_creation_reconciles_provenance_after_sync_observes_item(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        transaction_lock(conn)
        mission = create(conn, "mission", project_id=world.project["id"], number=1, title="Proof", objective="Prove theorem")
        run = create(conn, "run", mission_id=mission["id"], phase={"kind": "postprocessing"}, retry_policy={})
        operation = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
                           kind="forge_create", idempotency_key="create-issue", schema_version=1,
                           payload={"repository_id": str(world.repository["id"]), "origin_run_id": str(run["id"]),
                                    "review_phase": "postprocessing", "kind": "issue", "title": "Generalize", "body": "Improve API"})
        item = create(conn, "forge_item", repository_id=world.repository["id"], remote_number=3, kind="issue",
                      title="Generalize", status="open", observed_at=datetime.now(timezone.utc))

    def handler(request):
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 1})
        assert request.method == "GET", "already-created remote item must not be sent again"
        return httpx.Response(200, json=[{"id": 10, "number": 3, "title": "Generalize", "state": "open",
                             "body": "Improve API\n<!-- horizon-operation:" + str(operation["id"]) + " -->", "user": {"id": 1}}])

    assert manager(world, handler).dispatch_one() == "completed"
    with world.database.transaction() as conn:
        observed = get(conn, "forge_item", item["id"])
        assert observed["origin_run_id"] == run["id"]
        assert observed["review_phase"] == "postprocessing"
        assert get(conn, "outbox_operation", operation["id"])["result_ref_id"] is not None


def test_pr_creation_retries_labels_without_duplicate_pr():
    pulls, attached, posts = [], [], []
    labels = [{"id": 1, "name": "awaiting-review"}, {"id": 2, "name": "phase/postprocessing"}]
    failed = False

    def handler(request):
        nonlocal failed
        path = request.url.path
        if path.endswith("/user"):
            return httpx.Response(200, json={"id": 1})
        if path.endswith("/pulls"):
            if request.method == "GET":
                return httpx.Response(200, json=pulls)
            posts.append(request)
            pulls.append({**json.loads(request.content), "number": 1, "user": {"id": 1},
                          "head": {"sha": "a" * 40}})
            return httpx.Response(201, json=pulls[0])
        if path.endswith("/issues/1/labels"):
            if request.method == "GET":
                return httpx.Response(200, json=attached)
            value = json.loads(request.content)["labels"][0]
            attached.append(next(item for item in labels if item["id"] == value))
            if not failed:
                failed = True
                raise httpx.ReadTimeout("label accepted, response lost", request=request)
            return httpx.Response(200, json=attached)
        assert path.endswith("/labels")
        return httpx.Response(200, json=labels)

    client = ForgejoClient("https://forge.invalid", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    args = dict(kind="pull_request", title="Proof", body="Evidence", operation_id="create-1", head="proof", base="main",
                labels=tuple(item["name"] for item in labels))
    with pytest.raises(ConnectorFailure):
        client.create_item("owner/repo", **args)
    result = client.create_item("owner/repo", **args, reconcile_only=True)
    assert len(posts) == 1
    assert len(attached) == 2
    assert {item["name"] for item in result["labels"]} == set(args["labels"])


def test_pr_creation_uses_repository_phase_policy_attention_labels(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        transaction_lock(conn)
        mission = create(conn, "mission", project_id=world.project["id"], number=1, title="Proof", objective="Prove")
        run = create(conn, "run", mission_id=mission["id"], phase={"kind": "postprocessing"}, retry_policy={})
        policy = create(conn, "review_policy", project_id=world.project["id"], slug="library", phases=["postprocessing"],
                        instructions="Review", attention_labels=["needs-triage"])
        conn.execute(insert(tables["review_policy_repository"]).values(review_policy_id=policy["id"], repository_id=world.repository["id"]))
        operation = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
            kind="forge_create", schema_version=1, idempotency_key="policy-labels", payload={
                "repository_id": str(world.repository["id"]), "origin_run_id": str(run["id"]),
                "review_phase": "postprocessing", "kind": "pull_request"})
    state = manager(world, lambda request: httpx.Response(200))._operation_snapshot(dict(operation, _reconcile_only=False))
    assert state["creation_labels"] == ("needs-triage", "phase/postprocessing")


def test_public_label_contract_delivers_and_updates_projection(connector_world):
    from archon_horizon.pipeline.auth import Actor
    from archon_horizon.pipeline.review.decisions import queue_label
    world = connector_world
    labels = [{"id": 1, "name": "awaiting-review"}, {"id": 2, "name": "obsolete"}]
    attached = [labels[1]]
    with world.database.transaction() as conn:
        transaction_lock(conn)
        item = create(conn, "forge_item", repository_id=world.repository["id"], remote_number=1,
                      kind="pull_request", title="Proof", status="open", labels=["obsolete"], observed_at=datetime.now(timezone.utc))
        actor = Actor(world.actor["id"], "human", {"username": "maintainer"}, "api_key")
        operation = queue_label(conn, actor, {"forge_item_id": str(item["id"]),
            "add": ["awaiting-review"], "remove": ["obsolete"]}, "public-label-contract")

    def handler(request):
        if request.method == "DELETE":
            attached[:] = [value for value in attached if value["id"] != int(request.url.path.rsplit("/", 1)[1])]
            return httpx.Response(204)
        if request.url.path.endswith("/issues/1/labels"):
            if request.method == "POST":
                attached.extend(value for value in labels if value["id"] in json.loads(request.content)["labels"])
            return httpx.Response(200, json=attached)
        return httpx.Response(200, json=labels)

    assert manager(world, handler).dispatch_one() == "completed"
    assert attached == [labels[0]]
    with world.database.transaction() as conn:
        assert get(conn, "forge_item", item["id"])["labels"] == ["awaiting-review"]
        assert get(conn, "outbox_operation", operation["id"])["status"] == "completed"


def test_forge_projection_never_trusts_phase_labels_and_invalidates_old_gate(connector_world):
    world = connector_world
    head = "a" * 40

    def handler(request):
        if request.url.path == "/api/v1/repos/owner/repo":
            return httpx.Response(200, json={"has_issues": True, "has_pull_requests": True})
        if request.url.path.endswith("/issues"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[{"number": 1, "title": "Proof", "state": "open", "head": {"sha": head},
                                        "base": {"ref": "main"}, "user": {"id": 2}, "labels": [{"name": "phase/preprocessing"}]}])

    connector = manager(world, handler)
    connector.sync_forge(world.forge)
    with world.database.transaction() as conn:
        item = dict(conn.execute(select(tables["forge_item"])).mappings().one())
        assert item["review_phase"] is None
        policy = create(conn, "review_policy", project_id=world.project["id"], slug="policy", phases=["preprocessing"], instructions="Check")
        revision_id = snapshot(conn, "review_policy", policy, world.actor["id"])
        gate = create(conn, "review_gate", forge_item_id=item["id"], policy_id=policy["id"], policy_revision_id=revision_id)
    head = "b" * 40
    connector.sync_forge(world.forge)
    with world.database.transaction() as conn:
        assert get(conn, "review_gate", gate["id"])["status"] == "stale"
        assert get(conn, "forge_item", item["id"])["head_commit_oid"] == head


def test_reference_mirror_with_disabled_pulls_does_not_block_forge_sync(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        create(conn, "repository", project_id=world.project["id"], integration_id=world.forge["id"],
               slug="mirror", remote_id="2", remote_path="owner/mirror", default_branch="main", purpose="reference")

    def handler(request):
        if request.url.path in {"/api/v1/repos/owner/repo", "/api/v1/repos/owner/mirror"}:
            return httpx.Response(200, json={"has_issues": True, "has_pull_requests": not request.url.path.endswith("/mirror")})
        if request.url.path == "/api/v1/repos/owner/mirror/pulls":
            return httpx.Response(404)
        if request.url.path == "/api/v1/repos/owner/repo/pulls":
            return httpx.Response(200, json=[{"number": 1, "title": "Library contribution", "state": "open",
                                            "head": {"sha": "a" * 40}, "base": {"ref": "main"}}])
        return httpx.Response(200, json=[])

    manager(world, handler).sync_forge(world.forge)
    with world.database.transaction() as conn:
        cursor = conn.execute(select(tables["connector_cursor"]).where(
            tables["connector_cursor"].c.integration_id == world.forge["id"],
            tables["connector_cursor"].c.consumer == "forge")).mappings().one()
        assert cursor["status"] == "current"
        assert cursor["last_synced_at"] is not None
        assert conn.execute(select(tables["forge_item"].c.title)).scalar_one() == "Library contribution"


def test_zulip_backfill_edits_and_queue_expiry_preserve_cursor(connector_world):
    world = connector_world
    message = {"id": 1, "sender_id": 9, "sender_email": "worker@example.invalid", "timestamp": 1000,
               "content": "original", "stream_id": 1, "subject": "Proof"}
    events = []
    expired = False
    registrations = 0

    def handler(request):
        nonlocal registrations
        if request.url.path.endswith("/register"):
            registrations += 1
            return httpx.Response(200, json={"result": "success", "queue_id": f"queue-{registrations}", "last_event_id": -1})
        if request.url.path.endswith("/events"):
            if expired and request.url.params["queue_id"] == "queue-1":
                return httpx.Response(400, json={"result": "error", "code": "BAD_EVENT_QUEUE_ID"})
            return httpx.Response(200, json={"result": "success", "events": events})
        if request.url.path.endswith("/messages/1"):
            return httpx.Response(200, json={"result": "success", "message": message})
        return httpx.Response(200, json={"result": "success", "messages": [message], "found_newest": True})

    connector = manager(world, handler)
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        original = dict(conn.execute(select(tables["message"])).mappings().one())
    message = {**message, "content": "corrected", "last_edit_timestamp": 1001}
    events = [{"id": 10, "type": "update_message", "message_id": 1}]
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        edited = get(conn, "message", original["id"])
        assert edited["revision"] == original["revision"] + 1
        assert edited["body"] == "corrected"
        assert conn.execute(select(tables["connector_cursor"].c.last_event_remote_id)).scalar_one() == "10"
    expired = True
    connector.sync_zulip(world.zulip)
    with world.database.transaction() as conn:
        cursor = dict(conn.execute(select(tables["connector_cursor"])).mappings().one())
        assert cursor["queue_remote_id"] == "queue-2"
        assert cursor["status"] == "current"
        assert get(conn, "discussion", world.discussion["id"])["sync_status"] == "current"
        assert get(conn, "message", original["id"])["body"] == "corrected"


@pytest.mark.parametrize("edited_before_receipt", [False, True])
def test_durable_zulip_lost_ack_reconciles_without_new_post(connector_world, edited_before_receipt):
    world = connector_world
    stored = []
    posts = []

    def handler(request):
        if request.url.path == "/api/v1/messages/99":
            return httpx.Response(200, json={"message": stored[0]})
        if request.method == "GET":
            return httpx.Response(200, json={"messages": stored, "found_newest": True})
        posts.append(request)
        content = parse_qs(request.content.decode())["content"][0]
        if edited_before_receipt:
            content = "Correction after sending: " + content
        stored.append({"id": 99, "sender_id": 12, "timestamp": 1710000000,
                       "sender_email": "worker@example.invalid", "content": content})
        raise httpx.ReadTimeout("lost response", request=request)

    with world.database.transaction() as conn:
        mission = create(conn, "mission", project_id=world.project["id"], number=1, title="Definitions", objective="Choose API")
        run = create(conn, "run", mission_id=mission["id"], phase={"kind": "postprocessing"}, retry_policy={})
        assignment = create(conn, "assignment", run_id=run["id"], mission_id=mission["id"], number=1, queue_rank=1)
        artifact = save_blob(conn, world.service.store, world.project["id"], {"body": "Discuss the proof"})
        operation = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
                           kind="zulip_post", idempotency_key="reply-1", schema_version=1,
                           payload={"discussion_id": str(world.discussion["id"]), "source_assignment_id": str(assignment["id"]),
                                    "body_artifact_id": str(artifact["id"]), "read_messages": []})
    connector = manager(world, handler)
    assert connector.dispatch_one() == "uncertain"
    with world.database.transaction() as conn:
        conn.execute(update(tables["outbox_operation"]).where(tables["outbox_operation"].c.id == operation["id"]).values(retry_at=None))
    assert connector.dispatch_one() == "completed"
    assert len(posts) == 1
    with world.database.transaction() as conn:
        current = get(conn, "outbox_operation", operation["id"])
        reference = get(conn, "object_reference", current["result_ref_id"])
        posted = get(conn, "message", reference["message_id"])
        assert posted["remote_id"] == "99"
        assert posted["author_principal_id"] == world.actor["id"]
        assert posted["source_assignment_id"] == assignment["id"]
        receipt = conn.execute(select(tables["message_read"].c.message_revision).where(
            tables["message_read"].c.assignment_id == assignment["id"],
            tables["message_read"].c.message_id == posted["id"])).scalar_one_or_none()
        assert receipt == (None if edited_before_receipt else posted["revision"])


def test_review_acceptance_waits_for_remote_and_pins_policy(connector_world):
    world = connector_world
    with world.database.transaction() as conn:
        transaction_lock(conn)
        policy = create(conn, "review_policy", project_id=world.project["id"], slug="roadmap", phases=["formalization"], instructions="Review statements")
        conn.execute(insert(tables["review_policy_repository"]).values(review_policy_id=policy["id"], repository_id=world.repository["id"]))
        revision_id = snapshot(conn, "review_policy", policy, world.actor["id"])
        item = create(conn, "forge_item", repository_id=world.repository["id"], remote_number=1, kind="pull_request",
                      review_phase="formalization", target_branch="main", title="Statement", status="open",
                      head_commit_oid="a" * 40, observed_at=datetime.now(timezone.utc))
        operation = create(conn, "outbox_operation", project_id=world.project["id"], actor_principal_id=world.actor["id"],
                           kind="forge_review", idempotency_key="review-1", schema_version=1,
                           payload={"forge_item_id": str(item["id"]), "commit_oid": "a" * 40,
                                    "verdict": "approved", "summary": "The corrected statement matches the source.",
                                    "policy_revision_id": str(revision_id), "target_branch": "main"})
        assert conn.execute(select(tables["review_gate"])).first() is None

    def handler(request):
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"id": 7})
        if request.url.path.endswith("/reviews"):
            if request.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={"id": 12, "user": {"id": 7}, "state": "APPROVED", "commit_id": "a" * 40})
        return httpx.Response(200, json={"head": {"sha": "a" * 40}, "state": "open"})

    assert manager(world, handler).dispatch_one() == "completed"
    with world.database.transaction() as conn:
        gate = dict(conn.execute(select(tables["review_gate"])).mappings().one())
        assert gate["status"] == "accepted"
        assert gate["policy_revision_id"] == revision_id
        assert gate["accepted_commit_oid"] == "a" * 40
        assert get(conn, "outbox_operation", operation["id"])["status"] == "completed"
