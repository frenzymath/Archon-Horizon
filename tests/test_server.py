"""Live server: the service layer and the HTTP endpoints."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.server.app import create_server
from archon_horizon.server.service import WorkspaceService


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--name", "demo"])
    return ws


def test_service_state_and_inbox_edit(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)

    assert service.state()["workspace"] == "demo"
    assert service.state()["local_inbox"] == []

    created = service.edit_inbox("add", kind="hint", body="affine first")
    assert created["created"] == "I-0001"
    assert len(service.state()["local_inbox"]) == 1

    service.edit_inbox("complete", id="I-0001")
    assert service.state()["local_inbox"][0]["status"] == "completed"


def test_http_endpoints(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    server = create_server(ws, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port)

        conn.request("GET", "/api/state")
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["workspace"] == "demo"

        body = json.dumps({"action": "add", "kind": "hint", "body": "x"})
        conn.request("POST", "/api/inbox", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["created"] == "I-0001"

        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"<!doctype html>" in resp.read()
    finally:
        server.shutdown()
        server.server_close()
