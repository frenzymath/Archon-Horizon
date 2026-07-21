"""Abandoned keep-alive connections must not strand server threads.

The dashboard speaks HTTP/1.1, so a connection's thread parks waiting for the
next request. A peer that vanishes without a FIN or RST — a dropped SSH tunnel,
a suspended laptop — leaves a half-open socket that no TCP layer tears down, so
without a socket timeout that thread parks forever. Each drop stranded a
browser's whole connection pool, one thread and one fd apiece, until the process
could no longer spawn threads: still holding its port, no longer able to serve
it, forcing a restart on a fresh port.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.server.app import create_server


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    return ws


def _serve(tmp_path: Path, *, timeout: float):
    server = create_server(_workspace(tmp_path), "127.0.0.1", 0, None)
    # The shipped timeout is a minute; shrink it so the test is quick.
    server.RequestHandlerClass.timeout = timeout
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_handler_sets_a_socket_timeout() -> None:
    from archon_horizon.server.app import _make_handler

    handler = _make_handler(None, None)  # type: ignore[arg-type]
    # Without this, a half-open keep-alive connection parks its thread forever.
    assert isinstance(handler.timeout, (int, float))
    assert handler.timeout > 0
    # It must not cut the dashboard's own 5s poll while it sits idle between ticks.
    assert handler.timeout > 5


def test_abandoned_keep_alive_connections_are_reaped(tmp_path: Path) -> None:
    server, port = _serve(tmp_path, timeout=1)
    try:
        base = threading.active_count()
        held = []
        for _ in range(10):
            conn = socket.create_connection(("127.0.0.1", port))
            conn.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
            conn.recv(65536)
            held.append(conn)  # never closed, never spoken to again
        assert threading.active_count() - base == 10

        deadline = time.monotonic() + 15
        while threading.active_count() > base and time.monotonic() < deadline:
            time.sleep(0.1)
        assert threading.active_count() == base, "abandoned connections stranded their threads"

        # Reaping a dead peer must not disturb the server itself.
        live = socket.create_connection(("127.0.0.1", port))
        try:
            live.sendall(b"GET /api/state HTTP/1.1\r\nHost: x\r\n\r\n")
            assert b"200" in live.recv(65536)
        finally:
            live.close()
        for conn in held:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()
