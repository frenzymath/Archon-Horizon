"""Exercise the shipped integration proxy against disposable HTTP services."""

from contextlib import contextmanager
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
from threading import Thread
import time
from types import SimpleNamespace

import httpx
import pytest


IDS = {"forge": "a" * 32, "zulip": "b" * 32}
HOST = "browser-proxy.test"
ORIGIN = f"https://{HOST}"


def cookies(raw):
    parsed = SimpleCookie()
    # Go and Django ignore empty cookie segments left by proxy filtering.
    for segment in raw.split(";"):
        if segment.strip():
            parsed.load(segment.strip())
    return {name: value.value for name, value in parsed.items()}


@contextmanager
def http_service(respond):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            respond(self)

        def do_POST(self):
            respond(self)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def reply(handler, status, data=None, headers=None):
    body = json.dumps(data).encode() if data is not None else b""
    handler.send_response(status)
    for name, value in (headers or {}).items():
        handler.send_header(name, value)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


@pytest.fixture(scope="module")
def browser_proxy(tmp_path_factory):
    binary = os.environ.get("HORIZON_TEST_CADDY_BINARY") or shutil.which("caddy")
    if not binary:
        pytest.skip("set HORIZON_TEST_CADDY_BINARY to exercise the integration proxy")
    state = SimpleNamespace(revoked=False, auth_requests=[], native_requests=[])

    def auth(handler):
        headers = {name.lower(): value for name, value in handler.headers.items()}
        state.auth_requests.append((handler.path, headers))
        identity = next((kind for kind, identifier in IDS.items()
                         if handler.path.split("?", 1)[0] == f"/api/v3/integrations/{identifier}/browser-identity"), None)
        jar = cookies(headers.get("cookie", ""))
        if (state.revoked or identity is None or jar.get("horizon_session") != "signed-in"
                or jar.get(f"horizon_integration_{IDS[identity]}") != f"bound-{identity}"
                or jar.get("native_session") != "native-value"):
            reply(handler, 401, {"error": "session denied"})
        else:
            reply(handler, 204, headers={"X-Horizon-Integration-User": "trusted-operator"})

    def native(handler):
        body = handler.rfile.read(int(handler.headers.get("Content-Length", 0))).decode()
        request = {"method": handler.command, "path": handler.path, "body": body,
                   "headers": {name.lower(): value for name, value in handler.headers.items()}}
        state.native_requests.append(request)
        if (cookies(request["headers"].get("cookie", "")).get("native_session") != "native-value"
                and request["headers"].get("authorization") != "Basic valid-provider-credential"):
            reply(handler, 401, {"error": "provider credential denied"})
            return
        reply(handler, 200, request, {"X-Frame-Options": "DENY"})

    directory = tmp_path_factory.mktemp("browser-proxy")
    with http_service(auth) as auth_address, http_service(native) as native_address:
        # Hold both selected ports until the configuration is ready to launch.
        reservations = [socket.socket(), socket.socket()]
        try:
            for reservation in reservations:
                reservation.bind(("127.0.0.1", 0))
            ports = dict(zip(IDS, (item.getsockname()[1] for item in reservations)))
            template = Path(__file__).resolve().parents[1] / "deploy/pipeline/browser-integrations.Caddyfile"
            config = directory / "Caddyfile"
            config.write_text(template.read_text().replace("http://:9880", f"http://:{ports['forge']}")
                              .replace("http://:9881", f"http://:{ports['zulip']}"))
            env = {**os.environ, "HORIZON_BROWSER_HOST": HOST, "HORIZON_BROWSER_ORIGIN": ORIGIN,
                   "HORIZON_BROWSER_API": auth_address, "HORIZON_FORGE_UPSTREAM": native_address,
                   "HORIZON_ZULIP_UPSTREAM": native_address, "HORIZON_FORGE_INTEGRATION": IDS["forge"],
                   "HORIZON_ZULIP_INTEGRATION": IDS["zulip"], "XDG_DATA_HOME": str(directory / "data"),
                   "XDG_CONFIG_HOME": str(directory / "config"), "TMPDIR": str(directory)}
        finally:
            for reservation in reservations:
                reservation.close()
        log_path = directory / "caddy.log"
        with log_path.open("w") as log:
            process = subprocess.Popen([binary, "run", "--config", str(config), "--adapter", "caddyfile"],
                                       env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(trust_env=False, timeout=3) as client:
                    deadline = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            pytest.fail(f"Caddy exited during startup: {log_path.read_text()}")
                        try:
                            if all(client.get(f"http://127.0.0.1:{port}/", headers={"Host": HOST}).status_code == 401
                                   for port in ports.values()):
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            pytest.fail(f"Caddy did not start: {log_path.read_text()}")
                        time.sleep(0.05)
                    yield SimpleNamespace(client=client, ports=ports, state=state)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


@pytest.fixture
def proxy(browser_proxy):
    browser_proxy.state.revoked = False
    browser_proxy.state.auth_requests.clear()
    browser_proxy.state.native_requests.clear()
    return browser_proxy


def request(proxy, kind, *, cookie=None, method="GET", host=HOST, authorization=None, **kwargs):
    headers = {"Host": host, "X-WEBAUTH-USER": "spoofed-user", "X-WEBAUTH-EMAIL": "spoofed@example.test",
               "X-WEBAUTH-FULLNAME": "Spoofed Name", "X-Horizon-Integration-User": "spoofed-horizon"}
    if authorization is not None:
        headers["Authorization"] = authorization
    if cookie is not None:
        headers["Cookie"] = cookie
    return proxy.client.request(method, f"http://127.0.0.1:{proxy.ports[kind]}/native/path?test=1",
                                headers=headers, **kwargs)


def authenticated_cookie(kind):
    return (f"horizon_session=signed-in; horizon_integration_{IDS[kind]}=bound-{kind}; "
            "native_session=native-value; csrftoken=csrf-value")


@pytest.mark.parametrize("kind", IDS)
def test_anonymous_spoofed_and_unbound_requests_never_reach_native(proxy, kind):
    for cookie in (None, "horizon_session=signed-in; native_session=native-value",
                   authenticated_cookie(kind).replace("native-value", "swapped-native")):
        assert request(proxy, kind, cookie=cookie).status_code == 401
    assert proxy.state.native_requests == []
    assert all("authorization" not in headers for _, headers in proxy.state.auth_requests)


@pytest.mark.parametrize("kind", IDS)
@pytest.mark.parametrize("cookie_order", ["first", "middle", "last"])
def test_native_identity_and_credentials_are_isolated(proxy, kind, cookie_order):
    sensitive = (f"horizon_session=signed-in; horizon_integration_{IDS[kind]}=bound-{kind}; "
                 f"horizon_integration_{'c' * 32}=another-binding")
    raw = {"first": f"{sensitive}; native_session=native-value; csrftoken=csrf-value",
           "middle": f"native_session=native-value; {sensitive}; csrftoken=csrf-value",
           "last": f"native_session=native-value; csrftoken=csrf-value; {sensitive}"}[cookie_order]
    response = request(proxy, kind, cookie=raw, method="POST", content=b"native-form=preserved",
                       authorization="Bearer spoofed-token")
    assert response.status_code == 200, (response.text, [item["headers"].get("cookie") for item in proxy.state.native_requests])
    native = response.json()
    assert native["path"] == "/native/path?test=1" and native["method"] == "POST"
    assert native["body"] == "native-form=preserved"
    headers = native["headers"]
    assert "authorization" not in headers and "x-horizon-integration-user" not in headers
    assert "horizon_session" not in headers.get("cookie", "")
    assert "horizon_integration_" not in headers.get("cookie", "")
    assert cookies(headers["cookie"]) == {"native_session": "native-value", "csrftoken": "csrf-value"}
    assert headers["x-forwarded-proto"] == "https"
    if kind == "forge":
        assert headers["x-webauth-user"] == "trusted-operator"
        assert "x-webauth-email" not in headers and "x-webauth-fullname" not in headers
    assert "x-frame-options" not in response.headers
    assert f"frame-ancestors 'self' {ORIGIN}" in response.headers["content-security-policy"]


@pytest.mark.parametrize("kind", IDS)
def test_revocation_denies_subsequent_requests_on_each_port(proxy, kind):
    cookie = authenticated_cookie(kind)
    assert request(proxy, kind, cookie=cookie).status_code == 200
    accepted = len(proxy.state.native_requests)
    proxy.state.revoked = True
    assert request(proxy, kind, cookie=cookie).status_code == 401
    assert len(proxy.state.native_requests) == accepted


@pytest.mark.parametrize("kind", IDS)
def test_unknown_host_cannot_use_the_authentication_gateway(proxy, kind):
    assert request(proxy, kind, cookie=authenticated_cookie(kind), host="attacker.test").status_code == 403
    assert proxy.state.auth_requests == [] and proxy.state.native_requests == []


@pytest.mark.parametrize("kind", IDS)
def test_native_clients_use_provider_authentication_without_browser_cookies(proxy, kind):
    raw = f"native_session=stale-cookie; horizon_integration_{IDS[kind]}=old-binding"
    response = request(proxy, kind, cookie=raw, authorization="Basic valid-provider-credential")
    assert response.status_code == 200, response.text
    headers = response.json()["headers"]
    assert headers["authorization"] == "Basic valid-provider-credential"
    assert "cookie" not in headers and "x-horizon-integration-user" not in headers
    if kind == "forge":
        assert "x-webauth-user" not in headers and "x-webauth-email" not in headers
    assert proxy.state.auth_requests == []
    assert request(proxy, kind, authorization="Bearer invalid-provider-credential").status_code == 401
    assert proxy.state.auth_requests == []


@pytest.mark.parametrize("kind", IDS)
def test_provider_credentials_cannot_bypass_revoked_browser_session(proxy, kind):
    proxy.state.revoked = True
    response = request(proxy, kind, cookie=authenticated_cookie(kind), authorization="Basic valid-provider-credential")
    assert response.status_code == 401
    assert proxy.state.native_requests == []
