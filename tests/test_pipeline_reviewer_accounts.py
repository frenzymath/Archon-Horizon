from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4
import json
import stat

import httpx
import pytest

from archon_horizon.pipeline.integrations.connectors import ConnectorFailure, ForgejoClient, SecretResolver
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import change, create, get
from archon_horizon.pipeline.review.accounts import credentials, ensure_project_accounts, _write_secret
from archon_horizon.pipeline.worker.contracts import ExecutionGrant
from archon_horizon.pipeline.worker.transport import WorkerTransport
from test_pipeline_reviewer_invocations import review  # noqa: F401
from test_pipeline_service import service_database, world  # noqa: F401


def configure(world, review):
    integration = get(world.conn, "integration", get(world.conn, "repository", review["item"]["repository_id"])["integration_id"])
    change(world.conn, "repository", review["item"]["repository_id"], remote_path="project/library")
    resolver = SecretResolver(world.service.config.state_root)
    resolver.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_secret(resolver.root / "admin.json", {"token": "admin-private"})
    world.service.config.reviewer_account_admin_credentials = {integration["id"]: "secret:admin"}

    @contextmanager
    def transaction():
        yield world.conn

    return integration, resolver, SimpleNamespace(transaction=transaction)


@pytest.mark.parametrize("lost_token_reply", [False, True])
def test_provisions_once_with_private_recoverable_secret_and_read_repository_access(world, review, lost_token_reply):
    integration, resolver, database = configure(world, review)
    calls = []
    users = {}
    tokens = []
    lose_reply = lost_token_reply

    def respond(request):
        nonlocal lose_reply
        calls.append((request.method, request.url.path))
        path = request.url.path
        if "/tokens/" in path:
            assert request.headers["Authorization"].startswith("Basic ")
            assert request.method == "DELETE" and path.endswith("/23")
            tokens.clear()
            return httpx.Response(204)
        if path.endswith("/tokens"):
            assert request.headers["Authorization"].startswith("Basic ")
            if request.method == "GET":
                return httpx.Response(200, json=tokens)
            body = json.loads(request.content)
            assert body["scopes"] == ["write:repository", "read:user"]
            tokens.append({"id": 23, "name": body["name"]})
            if lose_reply:
                lose_reply = False
                return httpx.Response(500)
            return httpx.Response(201, json={"sha1": "reviewer-private"})
        assert request.headers["Authorization"] == "token admin-private"
        if path == "/api/v1/admin/users":
            body = json.loads(request.content)
            assert body["restricted"] and not body["must_change_password"]
            assert body["username"] == "horizon-review-source-review"
            users[body["username"]] = {"id": 17, "login": body["username"]}
            return httpx.Response(201, json=users[body["username"]])
        if request.method == "PUT":
            assert json.loads(request.content) == {"permission": "read"}
            return httpx.Response(204)
        name = path.rsplit("/", 1)[1]
        return httpx.Response(200, json=users[name]) if name in users else httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    factory = lambda endpoint, token: ForgejoClient(endpoint, token, client=client)
    if lost_token_reply:
        with pytest.raises(ConnectorFailure, match="http_500"):
            ensure_project_accounts(database, world.service, world.project["id"], resolver=resolver, client_factory=factory)
        assert get(world.conn, "reviewer_descriptor", review["descriptor"]["id"])["integration_identity_id"] is None
    result = ensure_project_accounts(database, world.service, world.project["id"], resolver=resolver, client_factory=factory)
    assert result == {"configured": [str(review["descriptor"]["id"])], "unconfigured": []}
    count = len(calls)
    assert ensure_project_accounts(database, world.service, world.project["id"], resolver=resolver, client_factory=factory) == result
    assert len(calls) == count
    assert calls.count(("POST", "/api/v1/admin/users")) == 1
    assert len(tokens) == 1
    descriptor = get(world.conn, "reviewer_descriptor", review["descriptor"]["id"])
    identity = get(world.conn, "integration_identity", descriptor["integration_identity_id"])
    assert identity["remote_user_id"] == "17"
    value = credentials(world.conn, review["actor"], review["execution"]["id"], world.service, resolver)
    assert len(value["accounts"]) == 1
    assert value["accounts"][0]["token"] == "reviewer-private"
    assert "password" not in value["accounts"][0]
    assert "admin-private" not in json.dumps(value)
    secret_path = resolver.root / (identity["credential_ref"].removeprefix("secret:") + ".json")
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_credentials_are_execution_scoped_and_regular_workers_get_none(world, review):
    integration, resolver, _database = configure(world, review)
    principal = create(world.conn, "principal", kind="service", service_name="review-account", display_name="Review account")
    identity = create(world.conn, "integration_identity", integration_id=integration["id"], principal_id=principal["id"],
        remote_user_id="17", credential_ref="secret:review")
    _write_secret(resolver.root / "review.json", {"token": "private", "password": "never-expose"})
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], integration_identity_id=identity["id"])
    create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="extra-not-in-defaults",
           functions=["reviewer"], instructions="Review definitions", integration_identity_id=identity["id"])
    assert len(credentials(world.conn, review["actor"], review["execution"]["id"], world.service, resolver)["accounts"]) == 2
    with pytest.raises(DomainError, match="another execution"):
        credentials(world.conn, review["actor"], uuid4(), world.service, resolver)
    with pytest.raises(DomainError, match="active execution"):
        credentials(world.conn, world.actor, review["execution"]["id"], world.service, resolver)
    change(world.conn, "assignment", review["assignment"]["id"], role="worker")
    assert credentials(world.conn, review["actor"], review["execution"]["id"], world.service, resolver)["accounts"] == []
    change(world.conn, "assignment", review["assignment"]["id"], reviewer_descriptor_id=review["descriptor"]["id"])
    assert len(credentials(world.conn, review["actor"], review["execution"]["id"], world.service, resolver)["accounts"]) == 1
    change(world.conn, "integration_identity", identity["id"], enabled=False)
    assert credentials(world.conn, review["actor"], review["execution"]["id"], world.service, resolver)["accounts"] == []


def test_no_admin_configuration_does_not_guess_or_use_regular_integration_key(world, review):
    _integration, resolver, database = configure(world, review)
    world.service.config.reviewer_account_admin_credentials = {}
    result = ensure_project_accounts(database, world.service, world.project["id"], resolver=resolver,
        client_factory=lambda *_: pytest.fail("No network without explicit administrator configuration"))
    assert result == {"configured": [], "unconfigured": [str(review["descriptor"]["id"])]}


@pytest.mark.parametrize("permission", [None, "read", "write"])
def test_refresh_adds_new_repository_access_without_downgrading_existing_permissions(world, review, permission):
    integration, resolver, database = configure(world, review)
    principal = create(world.conn, "principal", kind="service", service_name="review-account", display_name="Review account")
    identity = create(world.conn, "integration_identity", integration_id=integration["id"], principal_id=principal["id"],
        remote_user_id="17", credential_ref="secret:review")
    _write_secret(resolver.root / "review.json", {"token": "reviewer-private"})
    change(world.conn, "reviewer_descriptor", review["descriptor"]["id"], integration_identity_id=identity["id"])
    calls = []

    def respond(request):
        calls.append(request.method)
        if request.url.path == "/api/v1/user":
            assert request.headers["Authorization"] == "token reviewer-private"
            return httpx.Response(200, json={"id": 17, "login": "existing-reviewer"})
        assert request.headers["Authorization"] == "token admin-private"
        if request.url.path.endswith("/permission"):
            return httpx.Response(404) if permission is None else httpx.Response(200, json={"permission": permission})
        assert request.url.path.endswith("/collaborators/existing-reviewer")
        assert request.method == "PUT" and json.loads(request.content) == {"permission": "read"}
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(respond))
    ensure_project_accounts(database, world.service, world.project["id"], resolver=resolver, refresh_access=True,
        client_factory=lambda endpoint, token: ForgejoClient(endpoint, token, client=client))
    assert calls.count("PUT") == (1 if permission is None else 0)


def test_worker_download_uses_execution_token_without_host_credentials():
    grant = ExecutionGrant("execution", "assignment", 1, 60, "harness", "workspace", "/workspace", "repo", "Goal",
                           execution_token="execution-private")
    def respond(request):
        assert request.headers["Authorization"] == "Bearer execution-private"
        assert request.url.path == "/api/v3/executions/execution/reviewer-accounts"
        return httpx.Response(200, json={"execution_id": "execution", "accounts": [{"token": "review-private"}]})
    transport = WorkerTransport("http://testserver", "host-private", client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert transport.reviewer_accounts(grant)["accounts"][0]["token"] == "review-private"
