import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from types import SimpleNamespace
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest
from sqlalchemy import delete, insert, update

from archon_horizon.pipeline.integrations import browser_integrations
from archon_horizon.pipeline.api import create_app
from archon_horizon.pipeline.auth import Actor, issue_credential
from archon_horizon.pipeline.config import PipelineConfig
from archon_horizon.pipeline.persistence.records import create, transaction_lock
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api_database  # noqa: F401
from test_pipeline_service import make_world

HTTP_CLIENT = httpx.Client


@pytest.fixture
def browser(api_database, tmp_path):
    with api_database.transaction() as conn:
        transaction_lock(conn)
        world = make_world(conn, tmp_path)
        forge_id = world.workspace_repo["integration_id"]
        zulip = create(conn, "integration", kind="zulip", endpoint="http://zulip.internal", credential_ref="secret:bot")
        create(conn, "discussion", project_id=world.project["id"], integration_id=zulip["id"], channel_remote_id="1",
               topic="Formalization", observed_at=datetime.now(timezone.utc))
        credential, token = issue_credential(conn, world.actor.id, "browser_session", "Browser", datetime.now(timezone.utc) + timedelta(hours=1))
    config = PipelineConfig.model_validate({**world.service.config.model_dump(), "public_url": "https://horizon.test",
        "integration_public_urls": {forge_id: "https://horizon.test:3000", zulip["id"]: "https://horizon.test:3001"},
        "integration_browser_binding_credential_ref": "secret:browser-binding",
        "integration_browser_auth": {
            forge_id: {"mode": "forgejo_proxy", "identities": {world.actor.id: "operator"}, "session_cookie_name": "horizon_forge_test"},
            zulip["id"]: {"mode": "zulip_jwt", "identities": {world.actor.id: "operator@horizon.local"}, "credential_ref": "secret:browser-jwt"}}})
    secret_root = tmp_path / "secrets"
    secret_root.mkdir(mode=0o700)
    secret = secret_root / "browser-binding.json"
    secret.write_text(json.dumps({"signing_key": "server-only-test-key-" * 3}))
    secret.chmod(0o600)
    with TestClient(create_app(config, database=api_database, background=False), base_url=config.public_url) as client:
        client.cookies.set("horizon_session", token, domain="horizon.test", path="/")
        for identifier, name in [(forge_id, "horizon_forge_test"), (zulip["id"], "__Host-sessionid")]:
            with api_database.transaction() as conn:
                access = browser_integrations.authorize(conn, config, token, identifier)
            client.cookies.set(name, "test-native", domain="horizon.test", path="/")
            client.cookies.set(browser_integrations.binding_name(identifier),
                browser_integrations._binding_value(config, access, identifier, name, "test-native"), domain="horizon.test", path="/")
        yield SimpleNamespace(client=client, db=api_database, world=world, config=config, token=token, credential=credential,
                              forge_id=forge_id, zulip_id=zulip["id"])


def identity_path(identifier):
    return f"/api/v3/integrations/{identifier}/browser-identity"


def session_path(browser, identifier):
    return f"/api/v3/projects/{browser.world.project['id']}/integrations/{identifier}/web-session"


def mock_provider(monkeypatch, handler):
    monkeypatch.setattr(browser_integrations.httpx, "Client", lambda **options: HTTP_CLIENT(transport=httpx.MockTransport(handler), **options))
    monkeypatch.setattr(browser_integrations.SecretResolver, "__call__", lambda self, ref: {"jwt_auth_key": "private-test-key", "signing_key": "server-only-test-key-" * 3})


def test_browser_identity_and_configured_login_link(browser):
    response = browser.client.get(identity_path(browser.forge_id), headers={"X-WEBAUTH-USER": "attacker"})
    assert response.status_code == 204
    assert response.headers["X-Horizon-Integration-User"] == "operator"
    assert response.content == b"" and response.headers["cache-control"] == "no-store"
    response = browser.client.get(f"/api/v3/projects/{browser.world.project['id']}/integrations")
    assert response.status_code == 200
    assert all(row["browser_session"] for row in response.json()["items"])
    assert "jwt_auth_key" not in response.text and "credential_ref" not in response.text


@pytest.mark.parametrize("kind,credential_kind", [("human", "api_key"), ("service", "api_key"), ("agent", "execution_token")])
def test_non_browser_and_nonhuman_credentials_cannot_gain_native_identity(browser, monkeypatch, kind, credential_kind):
    if kind == "agent":
        monkeypatch.setattr(browser_integrations, "authenticate", lambda conn, token: Actor(uuid4(), "agent", {}, "execution_token"))
        assert browser.client.get(identity_path(browser.forge_id)).status_code == 403
        return
    with browser.db.transaction() as conn:
        principal = browser.world.actor.id
        if kind == "service":
            principal = create(conn, "principal", kind="service", service_name="sso_test_" + uuid4().hex, display_name="Service")["id"]
        _, token = issue_credential(conn, principal, credential_kind, "Must not become a human")
    browser.client.cookies.clear()
    browser.client.cookies.set("horizon_session", token, domain="horizon.test", path="/")
    assert browser.client.get(identity_path(browser.forge_id)).status_code == 403


def test_bearer_and_origin_bypasses_are_rejected(browser):
    assert browser.client.get(identity_path(browser.forge_id), headers={"Authorization": "Bearer " + browser.token}).status_code == 403
    path = session_path(browser, browser.forge_id)
    assert browser.client.post(path).status_code == 403
    assert browser.client.post(path, headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert browser.client.post(path, headers={"Authorization": "Bearer " + browser.token}).status_code == 403
    browser.client.cookies.clear()
    assert browser.client.get(identity_path(browser.forge_id)).status_code == 401


def test_mapping_enabled_and_project_association_are_required(browser):
    settings = browser.config.integration_browser_auth[browser.forge_id]
    settings.identities.clear()
    denied = browser.client.get(identity_path(browser.forge_id))
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "browser_identity_unmapped"
    settings.identities[browser.world.actor.id] = "operator"
    with browser.db.transaction() as conn:
        conn.execute(update(tables["integration"]).where(tables["integration"].c.id == browser.forge_id).values(enabled=False))
    assert browser.client.get(identity_path(browser.forge_id)).status_code == 403
    with browser.db.transaction() as conn:
        conn.execute(update(tables["integration"]).where(tables["integration"].c.id == browser.forge_id).values(enabled=True))
        other = create(conn, "project", slug="unrelated_" + uuid4().hex, title="Unrelated")
    path = f"/api/v3/projects/{other['id']}/integrations/{browser.forge_id}/web-session"
    assert browser.client.post(path, headers={"Origin": browser.config.public_url}).status_code == 403


def test_forward_auth_requires_access_to_a_linked_project(browser):
    with browser.db.transaction() as conn:
        conn.execute(delete(tables["system_grant"]).where(tables["system_grant"].c.principal_id == browser.world.actor.id))
    assert browser.client.get(identity_path(browser.zulip_id)).status_code == 403
    with browser.db.transaction() as conn:
        conn.execute(insert(tables["project_grant"]).values(principal_id=browser.world.actor.id,
                     project_id=browser.world.project["id"], role="viewer"))
    assert browser.client.get(identity_path(browser.zulip_id)).status_code == 204


def test_zulip_signs_short_lived_identity_and_returns_only_sanitized_native_cookies(browser, monkeypatch):
    def handler(request):
        assert browser.db.engine.pool.checkedout() == 0
        assert request.method == "POST" and str(request.url) == "http://zulip.internal/accounts/login/jwt/"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        token = parse_qs(request.content.decode())["token"][0]
        header, payload, signature = token.split(".")
        assert json.loads(base64.urlsafe_b64decode(header + "=="))["alg"] == "HS256"
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        assert claims["email"] == "operator@horizon.local"
        assert 55 <= claims["exp"] - datetime.now(timezone.utc).timestamp() <= 60
        assert base64.urlsafe_b64decode(signature + "=") == hmac.digest(b"private-test-key", (header + "." + payload).encode(), hashlib.sha256)
        return httpx.Response(302, headers=[("Location", "https://attacker.invalid"),
            ("Set-Cookie", "__Host-sessionid=fresh-session; Domain=attacker.invalid; Path=/bad; SameSite=None"),
            ("Set-Cookie", "__Host-csrftoken=csrf-value; Path=/; HttpOnly"),
            ("Set-Cookie", "horizon_session=attacker; Path=/")])
    mock_provider(monkeypatch, handler)
    result = browser.client.post(session_path(browser, browser.zulip_id), headers={"Origin": browser.config.public_url})
    assert result.status_code == 200, result.text
    assert result.json() == {"url": "https://horizon.test:3001", "mode": "zulip_jwt"}
    cookies = result.headers.get_list("set-cookie")
    assert len(cookies) == 3 and all("Secure" in value and "SameSite=Lax" in value and "Path=/" in value for value in cookies[:2])
    assert "SameSite=Strict" in cookies[2] and "HttpOnly" in cookies[2]
    assert "HttpOnly" in cookies[0] and "HttpOnly" not in cookies[1]
    assert all("Domain=" not in value and "horizon_session" not in value for value in cookies)
    assert "private-test-key" not in result.text and "fresh-session" not in result.text
    assert browser.client.cookies.get("horizon_session") == browser.token
    assert browser.client.get(identity_path(browser.zulip_id)).status_code == 204


def test_forge_replaces_stale_native_identity_and_forwards_only_mapped_header(browser, monkeypatch):
    browser.client.cookies.set("horizon_forge_test", "old-user-session", domain="horizon.test", path="/")
    def handler(request):
        assert request.method == "GET" and request.url.path == "/user/settings"
        assert request.headers["X-WEBAUTH-USER"] == "operator"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(200, headers=[("Set-Cookie", "horizon_forge_test=new-user-session; Path=/"), ("Set-Cookie", "_csrf=csrf")])
    mock_provider(monkeypatch, handler)
    response = browser.client.post(session_path(browser, browser.forge_id), headers={"Origin": browser.config.public_url, "X-WEBAUTH-USER": "attacker"})
    assert response.status_code == 200, response.text
    assert browser.client.cookies.get("horizon_forge_test") == "new-user-session"
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert browser.client.get(identity_path(browser.forge_id)).status_code == 204


def test_forge_login_binds_final_rotated_session(browser, monkeypatch):
    mock_provider(monkeypatch, lambda request: httpx.Response(200, headers=[
        ("Set-Cookie", "horizon_forge_test=anonymous; Path=/; Secure; HttpOnly"),
        ("Set-Cookie", "horizon_forge_test=authenticated; Path=/; Secure; HttpOnly"),
    ]))
    response = browser.client.post(session_path(browser, browser.forge_id), headers={"Origin": browser.config.public_url})
    assert response.status_code == 200, response.text
    assert browser.client.cookies.get("horizon_forge_test") == "authenticated"
    assert len(response.headers.get_list("set-cookie")) == 2
    assert browser.client.get(identity_path(browser.forge_id)).status_code == 204
    browser.client.cookies.set("horizon_forge_test", "anonymous", domain="horizon.test", path="/")
    assert browser.client.get(identity_path(browser.forge_id)).status_code == 403


@pytest.mark.parametrize("second", ["horizon_forge_test=other; Path=/other", "horizon_forge_test=other; Path=/; Domain=other.invalid"])
def test_forge_conflicting_session_scopes_are_rejected(browser, monkeypatch, second):
    mock_provider(monkeypatch, lambda request: httpx.Response(200, headers=[
        ("Set-Cookie", "horizon_forge_test=anonymous; Path=/"), ("Set-Cookie", second),
    ]))
    response = browser.client.post(session_path(browser, browser.forge_id), headers={"Origin": browser.config.public_url})
    assert response.status_code == 502
    assert not response.headers.get_list("set-cookie")


@pytest.mark.parametrize("change", ["native", "horizon", "binding", "mapping", "duplicate_native", "duplicate_horizon", "duplicate_headers"])
def test_forward_auth_binding_rejects_account_or_cookie_swaps(browser, change):
    headers = {}
    if change == "native":
        browser.client.cookies.set("horizon_forge_test", "someone-elses-session", domain="horizon.test", path="/")
    elif change == "horizon":
        with browser.db.transaction() as conn:
            _, token = issue_credential(conn, browser.world.actor.id, "browser_session", "New login", datetime.now(timezone.utc) + timedelta(hours=1))
        browser.client.cookies.set("horizon_session", token, domain="horizon.test", path="/")
    elif change == "binding":
        browser.client.cookies.delete(browser_integrations.binding_name(browser.forge_id), domain="horizon.test", path="/")
    elif change == "mapping":
        browser.config.integration_browser_auth[browser.forge_id].identities[browser.world.actor.id] = "other-operator"
    else:
        original = "; ".join(f"{cookie.name}={cookie.value}" for cookie in browser.client.cookies.jar)
        extra = "horizon_session=untrusted" if change == "duplicate_horizon" else "horizon_forge_test=untrusted"
        headers = [("Cookie", original), ("Cookie", extra)] if change == "duplicate_headers" else {"Cookie": extra + "; " + original}
    assert browser.client.get(identity_path(browser.forge_id), headers=headers).status_code == 403


@pytest.mark.parametrize("status", [302, 401, 403, 500])
def test_forge_rejects_login_redirect_or_error_even_with_session_cookie(browser, monkeypatch, status):
    mock_provider(monkeypatch, lambda request: httpx.Response(status, headers={"Set-Cookie": "horizon_forge_test=anonymous", "Location": "https://attacker.invalid"}))
    response = browser.client.post(session_path(browser, browser.forge_id), headers={"Origin": browser.config.public_url})
    assert response.status_code == 502 and not response.headers.get_list("set-cookie")
    assert "attacker.invalid" not in response.text


@pytest.mark.parametrize("headers", [[], [("Set-Cookie", "csrftoken=only-csrf")],
    [("Set-Cookie", "sessionid=")], [("Set-Cookie", "sessionid=x; Max-Age=0")],
    [("Set-Cookie", "sessionid=x"), ("Set-Cookie", "sessionid=y")],
    [("Set-Cookie", "sessionid=x; csrftoken=y")], [("Set-Cookie", 'sessionid="bad value"')],
    [("Set-Cookie", "sessionid=x; Expires=Thu, 01 Jan 1970 00:00:00 GMT")]])
def test_missing_or_malformed_provider_cookie_fails_without_setting_cookies(browser, monkeypatch, headers):
    mock_provider(monkeypatch, lambda request: httpx.Response(200, headers=headers))
    response = browser.client.post(session_path(browser, browser.zulip_id), headers={"Origin": browser.config.public_url})
    assert response.status_code == 502
    assert not response.headers.get_list("set-cookie")


def test_provider_timeout_is_sanitized(browser, monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("private upstream diagnostic and JWT must not be exposed")
    mock_provider(monkeypatch, handler)
    response = browser.client.post(session_path(browser, browser.zulip_id), headers={"Origin": browser.config.public_url})
    assert response.status_code == 502 and "private upstream" not in response.text
    assert not response.headers.get_list("set-cookie")


@pytest.mark.parametrize("revoke", ["credential", "project", "integration"])
def test_access_is_rechecked_after_provider_network_request(browser, monkeypatch, revoke):
    with browser.db.transaction() as conn:
        conn.execute(delete(tables["system_grant"]).where(tables["system_grant"].c.principal_id == browser.world.actor.id))
        conn.execute(insert(tables["project_grant"]).values(principal_id=browser.world.actor.id,
                     project_id=browser.world.project["id"], role="viewer"))
    def handler(request):
        with browser.db.transaction() as conn:
            if revoke == "credential":
                conn.execute(update(tables["credential"]).where(tables["credential"].c.id == browser.credential["id"]).values(revoked_at=datetime.now(timezone.utc)))
            elif revoke == "project":
                conn.execute(delete(tables["project_grant"]).where(tables["project_grant"].c.principal_id == browser.world.actor.id))
            else:
                conn.execute(update(tables["integration"]).where(tables["integration"].c.id == browser.zulip_id).values(enabled=False))
        return httpx.Response(302, headers={"Set-Cookie": "sessionid=fresh"})
    mock_provider(monkeypatch, handler)
    response = browser.client.post(session_path(browser, browser.zulip_id), headers={"Origin": browser.config.public_url})
    assert response.status_code in {401, 403}
    assert not response.headers.get_list("set-cookie")


def test_browser_auth_configuration_requires_explicit_secure_same_host_origins(browser):
    original = browser.config.model_dump()
    for changes in [{"secure_cookies": False}, {"public_url": "http://127.0.0.1:8788"},
                    {"integration_public_urls": {browser.forge_id: "https://elsewhere.invalid", browser.zulip_id: "https://horizon.test:3001"}},
                    {"integration_public_urls": {}}]:
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate({**original, **changes})
    assert PipelineConfig.model_validate({**original, "integration_browser_auth": {}}).integration_browser_auth == {}
    for entry in [{"mode": "forgejo_proxy", "identities": {browser.world.actor.id: "operator\r\nX: bad"}},
                  {"mode": "forgejo_proxy", "session_cookie_name": "horizon_session"},
                  {"mode": "zulip_jwt", "credential_ref": "/tmp/key"}]:
        with pytest.raises(ValidationError):
            PipelineConfig.model_validate({**original, "integration_browser_auth": {browser.forge_id: entry}})
