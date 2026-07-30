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
import gzip
import hashlib
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from archon_horizon.log import log

from .service import WorkspaceService


def dashboard_policy_rows(service: WorkspaceService | None) -> dict[str, str]:
    """Compact scheduling/write-policy metadata for the startup table."""
    if service is None:
        return {}
    cfg = service.cfg
    project_file_rules = sum(len(project.freeze_files) for project in cfg.projects.values())
    project_declaration_rules = sum(
        len(project.freeze_declarations) for project in cfg.projects.values()
    )
    other: list[str] = []
    if cfg.freeze_agents:
        other.append(f"agents: {', '.join(cfg.freeze_agents)}")
    file_count = len(cfg.freeze_files) + project_file_rules
    if file_count:
        other.append(f"files: {file_count}")
    declaration_count = len(cfg.freeze_declarations) + project_declaration_rules
    if declaration_count:
        other.append(f"declarations: {declaration_count}")
    if cfg.freeze_blueprint_nodes:
        other.append(f"blueprint nodes: {len(cfg.freeze_blueprint_nodes)}")
    return {
        "Frozen projects": ", ".join(cfg.freeze_projects) or "none",
        "Other freezes": " · ".join(other) or "none",
        "Max parallel": str(cfg.scheduler.max_parallel_sessions),
    }


def _make_handler(service: WorkspaceService, dist_dir: Path | None) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # Reap abandoned connections. HTTP/1.1 means keep-alive, so a connection's
        # thread parks in `readline()` waiting for the next request — and without a
        # timeout it parks *forever* when the peer vanishes without a FIN or RST.
        # That is exactly what a dropped SSH tunnel (or a suspended laptop) leaves
        # behind: a half-open socket no TCP layer will ever tear down. Each drop
        # stranded a browser's whole connection pool, one thread and one fd apiece,
        # until the process could no longer spawn threads — still holding the port,
        # no longer able to serve it, which forced a restart on a fresh port.
        #
        # Well above the dashboard's 5s poll (an idle keep-alive between polls must
        # never be cut) and above a slow transfer of the largest payload, but short
        # enough that a dead tunnel's threads are returned promptly. On timeout,
        # `handle_one_request` closes the connection; a live browser just reconnects.
        timeout = 60

        def log_message(self, *args: object) -> None:
            return

        # Browsers routinely open speculative keep-alive connections and drop them
        # (or abort an in-flight poll when the tab navigates/refreshes). That aborts
        # the socket read/flush with ECONNRESET/EPIPE, which socketserver would
        # otherwise dump as a full traceback per connection — pure noise, not a
        # real failure. Swallow those benign resets across the whole request
        # lifecycle (the requestline read in handle(), the flush in finish()).
        # A `TimeoutError` from the `timeout` above is the same kind of non-event —
        # a peer that stopped talking — and carries no errno, so it is matched by
        # type rather than by number.
        @staticmethod
        def _benign_conn_error(exc: BaseException) -> bool:
            if isinstance(exc, (BrokenPipeError, ConnectionResetError, TimeoutError)):
                return True
            return isinstance(exc, OSError) and exc.errno in (errno.EPIPE, errno.ECONNRESET)

        def handle(self) -> None:
            try:
                super().handle()
            except Exception as exc:  # noqa: BLE001 - re-raise anything unexpected
                if not self._benign_conn_error(exc):
                    raise
                self.close_connection = True

        def finish(self) -> None:
            try:
                super().finish()
            except Exception as exc:  # noqa: BLE001 - re-raise anything unexpected
                if not self._benign_conn_error(exc):
                    raise

        def _send(self, code: int, body: bytes, content_type: str, cache_control: str | None = None) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                if cache_control is not None:
                    self.send_header("Cache-Control", cache_control)
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

        def _send_304(self, etag: str) -> None:
            try:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Content-Length", "0")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError as exc:
                if exc.errno not in (errno.EPIPE, errno.ECONNRESET):
                    raise

        def _api_response(self, obj: object, etag: str | None = None) -> None:
            """Send a GET /api/* JSON payload with conditional-GET + gzip.

            The dashboard polls /api/state every 5s and the payload can be several
            MB. An ETag lets an unchanged poll return a tiny ``304 Not Modified``
            instead of re-sending the whole body, and gzip shrinks the payload
            (~10x for this JSON) when it *does* change. Both are safe/generic for
            every /api/* GET, so this is not special-cased to /api/state.
            ``etag`` overrides the body hash with a precomputed validator (the
            /api/state change stamp).
            """
            body = json.dumps(obj).encode("utf-8")
            if etag is None:
                etag = '"' + hashlib.sha256(body).hexdigest() + '"'
            if self.headers.get("If-None-Match") == etag:
                self._send_304(etag)
                return
            encoding: str | None = None
            # Only worth compressing a payload big enough to beat the CPU/overhead;
            # tiny endpoints (a few hundred bytes) are sent as-is.
            if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 1024:
                body = gzip.compress(body, compresslevel=6)
                encoding = "gzip"
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("ETag", etag)
                if encoding:
                    self.send_header("Content-Encoding", encoding)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return
            except OSError as exc:
                if exc.errno not in (errno.EPIPE, errno.ECONNRESET):
                    raise

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
                # No built SPA (a source checkout without `npm run build`): the
                # /api/* endpoints still serve; the page just says how to build.
                if route == "/":
                    notice = (
                        "<!doctype html><meta charset='utf-8'><title>Archon Horizon</title>"
                        "<body style='font-family:system-ui;max-width:640px;margin:80px auto'>"
                        "<h1>Dashboard frontend not built</h1>"
                        "<p>This install has no packaged <code>frontend/dist</code>. Build it with:</p>"
                        "<pre>cd src/archon_horizon/frontend && npm install && npm run build</pre>"
                        "<p>The JSON API is live at <a href='/api/state'>/api/state</a>.</p></body>"
                    )
                    self._send(200, notice.encode("utf-8"), "text/html; charset=utf-8")
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
            served_index = target.resolve() == (dist_dir / "index.html").resolve()
            if rel.startswith("assets/") and not served_index:
                # Vite emits content-hashed filenames, so these are safe to cache forever.
                cache_control = "public, max-age=31536000, immutable"
            else:
                # index.html / SPA fallback must be refetched so new asset hashes are picked up.
                cache_control = "no-store"
            self._send(200, target.read_bytes(), ctype, cache_control=cache_control)

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            try:
                if route == "/api/state":
                    # Short-circuit BEFORE the (expensive) state computation: the
                    # stamp is a cheap stat-walk over everything state() reads, so
                    # an unchanged 5s poll costs ~1ms instead of a full recompute.
                    etag = 'W/"' + service.state_stamp() + '"'
                    if self.headers.get("If-None-Match") == etag:
                        self._send_304(etag)
                        return
                    self._api_response(service.serve_endpoint(self.path), etag=etag)
                elif route.startswith("/api/"):
                    self._api_response(service.serve_endpoint(self.path))
                else:
                    self._serve_asset(route)
            except KeyError:
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

        def _origin_ok(self) -> bool:
            """Reject cross-site writes.

            These endpoints mutate the workspace (inbox, roadmap, tasks) with no
            credentials of any kind, so the browser's same-origin policy is the
            only thing standing between them and any page the user happens to
            have open. It is not enough on its own: a ``text/plain`` POST is a
            CORS "simple request", so it is sent cross-origin with no preflight
            and the attacker never needs to read the response to have already
            written to the inbox — which agents consume as instructions.

            A same-origin fetch from the dashboard sends ``Origin`` matching the
            host we are serving. Anything else (a foreign Origin) is refused. A
            *missing* Origin is allowed: non-browser clients (curl, the tests)
            omit it, and they were never the threat here.
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
            # Same host family, but a different port is still a different origin.
            # Compare against the port we were actually reached on (the Host
            # header), so this holds whichever --port the dashboard bound.
            host_header = self.headers.get("Host") or ""
            expected = urlparse(f"//{host_header}").port
            return parsed.port == expected or (parsed.port is None and expected in (None, 80))

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route not in ("/api/inbox", "/api/roadmap", "/api/tasks", "/api/blueprint/sync"):
                self._json({"error": "not found"}, 404)
                return
            if not self._origin_ok():
                self._json({"error": "cross-origin request refused"}, 403)
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
    service = getattr(server, "_horizon_service", None)
    rows = {
        "Mode": mode,
        "URL": url,
        "Workspace": str(root),
        **dashboard_policy_rows(service),
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
