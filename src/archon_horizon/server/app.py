"""Live dashboard HTTP server (stdlib only).

Serves the dashboard and a small JSON API the SPA polls: current state, the
list of agent transcripts, a single transcript, and a POST endpoint to edit
the local inbox. GET ``/api/*`` is routed through the same
``service.serve_endpoint`` the static exporter uses, so live and static can't
drift. If a built SPA (``dist_dir``) is present it is served from ``/``;
otherwise the single-file Python dashboard is served as a fallback.
"""

from __future__ import annotations

import json
import mimetypes
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
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: object, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode("utf-8"), "application/json")

        def _serve_asset(self, route: str) -> None:
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
            if urlparse(self.path).path != "/api/inbox":
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
                action = payload.pop("action")
                self._json(service.edit_inbox(action, **payload))
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

    return Handler


def create_server(
    root: Path, host: str = "127.0.0.1", port: int = 8765, dist_dir: Path | None = None
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _make_handler(WorkspaceService(root), dist_dir))


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765, dist_dir: Path | None = None) -> None:
    server = create_server(root, host, port, dist_dir)
    mode = "SPA" if dist_dir else "Python-rendered"
    log.header("Archon Horizon Dashboard")
    log.key_value({
        "Mode": mode,
        "URL": f"http://{host}:{server.server_address[1]}",
        "Workspace": str(root),
    })
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
