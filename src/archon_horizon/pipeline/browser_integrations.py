"""Explicit human browser identities for independently hosted native clients."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
import json
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select, union

from .auth import authenticate, is_admin, require_project
from .connectors import ConnectorFailure, SecretResolver
from .errors import DomainError
from .records import get
from .schema import tables


def authorize(conn, config, token, integration_id, *, project_id=None, authorization=None):
    if authorization:
        raise DomainError("browser_session_required", "Use a human Horizon browser session to open this integration", 403)
    actor = authenticate(conn, token)
    if actor.kind != "human" or actor.credential_kind != "browser_session":
        raise DomainError("browser_session_required", "Use a human Horizon browser session to open this integration", 403)
    integration = get(conn, "integration", integration_id)
    settings = config.integration_browser_auth.get(integration_id)
    if not integration["enabled"] or settings is None:
        raise DomainError("browser_login_unavailable", "Browser sign-in is not enabled for this integration", 403)
    expected = "forge" if settings.mode == "forgejo_proxy" else "zulip"
    if integration["kind"] != expected:
        raise DomainError("browser_login_configuration", "The integration browser login mode needs operator configuration", 503)
    identity = settings.identities.get(actor.id)
    if not identity:
        raise DomainError("browser_identity_unmapped", "Ask the operator to map your Horizon account to this integration", 403)
    repository, discussion = tables["repository"], tables["discussion"]
    linked = union(select(repository.c.project_id).where(repository.c.integration_id == integration_id,
                    repository.c.archived_at.is_(None)),
                   select(discussion.c.project_id).where(discussion.c.integration_id == integration_id)).subquery()
    if project_id is not None:
        require_project(conn, actor, project_id)
        if not conn.execute(select(linked.c.project_id).where(linked.c.project_id == project_id)).first():
            raise DomainError("integration_project_mismatch", "This integration is not connected to the selected project", 403)
    elif not is_admin(conn, actor):
        grant = tables["project_grant"]
        if not conn.execute(select(linked.c.project_id).join(grant, grant.c.project_id == linked.c.project_id)
                            .where(grant.c.principal_id == actor.id).limit(1)).first():
            raise DomainError("forbidden", "This account has no project access to this integration", 403)
    return {"principal_id": actor.id, "credential_id": actor.credential_id,
            "integration_revision": integration["revision"], "endpoint": integration["endpoint"],
            "identity": identity, "mode": settings.mode, "credential_ref": settings.credential_ref,
            "session_cookie_name": settings.session_cookie_name or "i_like_gitea",
            "url": config.integration_public_urls[integration_id].rstrip("/")}


def _jwt(identity, key):
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    payload = encode({"alg": "HS256", "typ": "JWT"}) + b"." + encode({"email": identity, "exp": int(time.time()) + 60})
    return (payload + b"." + base64.urlsafe_b64encode(hmac.digest(key.encode(), payload, hashlib.sha256)).rstrip(b"=")).decode()


def binding_name(integration_id):
    return "horizon_integration_" + integration_id.hex


def _binding_value(config, access, integration_id, name, value):
    try:
        key = SecretResolver(config.state_root)(config.integration_browser_binding_credential_ref).get("signing_key", "")
        if len(key.encode()) < 32:
            raise ValueError("binding key too short")
    except (ConnectorFailure, ValueError):
        raise DomainError("browser_login_configuration", "The browser integration signing key needs operator configuration", 503) from None
    payload = json.dumps([str(access["credential_id"]), str(integration_id), access["identity"], name, value], separators=(",", ":")).encode()
    return name + "." + hmac.digest(key.encode(), payload, hashlib.sha256).hex()


def verify_binding(config, access, integration_id, cookies, cookie_headers=()):
    binding = cookies.get(binding_name(integration_id), "")
    name = binding.partition(".")[0]
    allowed = {access["session_cookie_name"]} if access["mode"] == "forgejo_proxy" else {"sessionid", "__Host-sessionid"}
    protected = allowed | {binding_name(integration_id), "horizon_session"}
    seen = set()
    for header in cookie_headers:
        for part in header.split(";"):
            key = part.partition("=")[0].strip()
            if key in protected:
                if key in seen:
                    raise DomainError("browser_login_required", "Clear duplicate integration cookies and reconnect from Horizon", 403)
                seen.add(key)
    native = cookies.get(name, "") if name in allowed else ""
    if not native or len(native) > 1024 or len(binding) > 200 or not hmac.compare_digest(
            binding.encode(), _binding_value(config, access, integration_id, name, native).encode()):
        raise DomainError("browser_login_required", "Open this integration from Horizon to sign in with your current account", 403)


def bind_cookies(config, access, integration_id, cookies):
    sessions = {access["session_cookie_name"]} if access["mode"] == "forgejo_proxy" else {"sessionid", "__Host-sessionid"}
    for raw in cookies:
        jar = SimpleCookie()
        jar.load(raw)
        for name, native in jar.items():
            if name in sessions:
                binding = SimpleCookie()
                key = binding_name(integration_id)
                binding[key] = _binding_value(config, access, integration_id, name, native.value)
                item = binding[key]
                item["secure"], item["httponly"], item["path"], item["samesite"] = True, True, "/", "Strict"
                return [*cookies, item.OutputString()]
    raise DomainError("browser_login_failed", "The provider did not return a browser session", 502)


def _cookies(headers, *, sessions=frozenset({"sessionid", "__Host-sessionid"}), csrf=frozenset({"csrftoken", "__Host-csrftoken"}), allow_session_rotation=False):
    allowed = sessions | csrf
    found = {}
    scopes = {}
    if len(headers) > 32:
        raise ValueError("too many cookies")
    for raw in headers:
        if len(raw) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in raw):
            raise ValueError("invalid cookie header")
        jar = SimpleCookie()
        jar.load(raw)
        name = raw.split("=", 1)[0].strip()
        if name not in allowed:
            continue
        if len(jar) != 1 or name not in jar:
            raise ValueError("ambiguous cookie")
        original = jar[name]
        scope = (original["domain"], original["path"])
        if name in found and not (allow_session_rotation and name in sessions and scopes[name] == scope):
            raise ValueError("ambiguous cookie")
        value = original.value
        if not value or len(value) > 1024 or any(ord(char) < 33 or ord(char) > 126 or char in '\",;\\' for char in value):
            raise ValueError("invalid cookie value")
        if original["max-age"] and (not original["max-age"].isdecimal() or int(original["max-age"]) <= 0):
            raise ValueError("expired cookie")
        if original["expires"] and not original["max-age"]:
            expires = parsedate_to_datetime(original["expires"])
            if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                raise ValueError("expired cookie")
        sanitized = SimpleCookie()
        sanitized[name] = value
        cookie = sanitized[name]
        cookie["path"], cookie["secure"], cookie["samesite"] = "/", True, "Lax"
        cookie["httponly"] = name in sessions
        # Browser-session cookies never outlive a browser restart because of upstream persistence directives.
        found[name] = cookie.OutputString()
        scopes[name] = scope
    if len(sessions.intersection(found)) != 1:
        raise ValueError("missing or ambiguous session cookie")
    return list(found.values())


def provider_cookies(config, access):
    endpoint = urlsplit(access["endpoint"])
    if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise DomainError("browser_login_configuration", "The integration endpoint needs operator configuration", 503)
    try:
        # No caller headers, credentials, redirects, proxy environment, or response body are forwarded.
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False) as client:
            if access["mode"] == "forgejo_proxy":
                with client.stream("GET", access["endpoint"].rstrip("/") + "/user/settings",
                                   headers={"X-WEBAUTH-USER": access["identity"]}) as response:
                    if response.status_code != 200:
                        raise ValueError("login rejected")
                    # Forgejo creates an anonymous session before rotating it on login.
                    # Same-scope Set-Cookie headers replace earlier values in wire order.
                    return _cookies(response.headers.get_list("set-cookie"),
                                    sessions={access["session_cookie_name"]}, csrf={"_csrf"}, allow_session_rotation=True)
            key = SecretResolver(config.state_root)(access["credential_ref"]).get("jwt_auth_key")
            if not key:
                raise ValueError("missing JWT key")
            with client.stream("POST", access["endpoint"].rstrip("/") + "/accounts/login/jwt/",
                               data={"token": _jwt(access["identity"], key)}) as response:
                if response.status_code not in {200, 302, 303}:
                    raise ValueError("login rejected")
                return _cookies(response.headers.get_list("set-cookie"))
    except (ConnectorFailure, httpx.HTTPError, CookieError, ValueError, OverflowError):
        raise DomainError("browser_login_failed", "Integration sign-in failed; check the mapped account and provider login configuration, then retry", 502) from None


def establish(db, config, token, integration_id, project_id, *, authorization=None):
    with db.transaction() as conn:
        access = authorize(conn, config, token, integration_id, project_id=project_id, authorization=authorization)
    cookies = provider_cookies(config, access)
    # Access may be revoked or the integration changed while the provider was answering.
    with db.transaction() as conn:
        current = authorize(conn, config, token, integration_id, project_id=project_id, authorization=authorization)
        if current != access:
            raise DomainError("browser_login_changed", "Integration access changed; sign in again", 409)
    return {"url": access["url"], "mode": access["mode"]}, bind_cookies(config, access, integration_id, cookies)
