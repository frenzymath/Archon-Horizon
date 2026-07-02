"""Live dashboard HTTP server (stdlib only).

Serves the dashboard and a small JSON API the SPA polls: current state, the
list of agent transcripts, a single transcript, and a POST endpoint to edit
the local inbox. GET ``/api/*`` is routed through the same
``service.serve_endpoint`` the static exporter uses, so live and static can't
drift. If a built SPA (``dist_dir``) is present it is served from ``/``;
otherwise the single-file Python dashboard is served as a fallback.
"""

from __future__ import annotations

import errno
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from archon_horizon.log import log

from .service import WorkspaceService


def _make_handler(service: WorkspaceService, dist_dir: Path | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError as exc:
                if exc.errno in (errno.EPIPE, errno.ECONNRESET):
                    return
                raise

        def _json(self, obj: object, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _serve_asset(self, route: str) -> None:
            if route.startswith("/reports/") and route.endswith(".md"):
                report = route.removeprefix("/reports/").removesuffix(".md")
                target = (service.workspace.state_path / "reports" / f"{report}.md").resolve()
                reports_dir = (service.workspace.state_path / "reports").resolve()
                if reports_dir in target.parents and target.is_file():
                    self._send(200, target.read_bytes(), "text/markdown; charset=utf-8")
                else:
                    self._json({"error": "not found"}, 404)
                return
            if dist_dir is None:
                if route == "/":
                    self._send(200, service.render_html(live=True).encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._json({"error": "not found"}, 404)
                return
            rel = "index.html" if route == "/" else route.lstrip("/")
            target = (dist_dir / rel).resolve()
            if dist_dir.resolve() not in target.parents and target != (dist_dir / "index.html").resolve():
                target = dist_dir / "index.html"  # SPA fallback / traversal guard
            if not target.is_file():
                target = dist_dir / "index.html"
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            try:
                if route.startswith("/api/"):
                    self._json(service.serve_endpoint(self.path))
                else:
                    self._serve_asset(route)
            except KeyError:
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route not in ("/api/inbox", "/api/roadmap", "/api/tasks", "/api/blueprint/sync"):
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                if route == "/api/blueprint/sync":
                    self._json(service.sync_blueprint_dags())
                    return
                action = payload.pop("action")
                if route == "/api/inbox":
                    self._json(service.edit_inbox(action, **payload))
                elif route == "/api/roadmap":
                    self._json(service.edit_roadmap(action, **payload))
                elif route == "/api/tasks":
                    self._json(service.edit_task(action, **payload))
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

    return Handler


def dashboard_url(host: str, port: int) -> str:
    """Return a browser URL for a bound dashboard host."""
    if host == "0.0.0.0":
        return f"http://localhost:{port}"
    if host == "::":
        return f"http://[::1]:{port}"
    if ":" in host and not host.startswith("["):
        return f"http://[{host}]:{port}"
    return f"http://{host}:{port}"


def create_server(
    root: Path, host: str = "127.0.0.1", port: int = 8765, dist_dir: Path | None = None
) -> ThreadingHTTPServer:
    service = WorkspaceService(root)
    server = ThreadingHTTPServer((host, port), _make_handler(service, dist_dir))
    # Stash the service so serve_server can pre-warm the (slow-to-build) search
    # index in the background, once, after the port is actually bound.
    server._horizon_service = service  # type: ignore[attr-defined]
    return server


def create_server_with_fallback(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    dist_dir: Path | None = None,
    *,
    tries: int = 26,
) -> ThreadingHTTPServer:
    """Bind ``port`` or the next available port in the following range.

    ``tries=26`` means the requested port plus the next 25 ports. Port ``0``
    remains the OS-assigned ephemeral-port mode and is tried only once.
    """
    if port == 0:
        return create_server(root, host, port, dist_dir)
    last: OSError | None = None
    for candidate in range(port, port + max(tries, 1)):
        try:
            return create_server(root, host, candidate, dist_dir)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last = exc
    raise last or OSError(f"could not bind {host}:{port}")


def serve_server(
    server: ThreadingHTTPServer,
    *,
    root: Path,
    host: str,
    requested_port: int,
    dist_dir: Path | None = None,
) -> None:
    mode = "SPA" if dist_dir else "Python-rendered"
    actual_port = server.server_address[1]
    if requested_port != 0 and actual_port != requested_port:
        log.warn(f"Port {requested_port} was busy; serving dashboard on {actual_port}.")
    url = dashboard_url(host, actual_port)
    log.header("Archon Horizon Dashboard")
    rows = {
        "Mode": mode,
        "URL": url,
        "Workspace": str(root),
    }
    if host in ("0.0.0.0", "::"):
        rows["Bind"] = f"{host}:{actual_port}"
        rows["Remote"] = f"http://<server-host>:{actual_port}"
        log.warn(
            "Dashboard is bound to all interfaces and may be reachable from other machines "
            "if your firewall allows it."
        )
    log.key_value(rows)
    if host in ("127.0.0.1", "localhost", "::1"):
        log.info(
            "On some remote VMs, containers, or port-forwarded sessions, localhost forwarding "
            "may fail; rerun with `horizon dashboard --public --port "
            f"{actual_port}` if the dashboard does not open."
        )
    service = getattr(server, "_horizon_service", None)
    if service is not None:
        # Build the search index ahead of the first query so search is snappy.
        threading.Thread(target=service.warm_search_index, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, dist_dir: Path | None = None) -> None:
    server = create_server_with_fallback(root, host, port, dist_dir)
    serve_server(server, root=root, host=host, requested_port=port, dist_dir=dist_dir)
