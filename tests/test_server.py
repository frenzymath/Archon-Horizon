"""Live server: the service layer and the HTTP endpoints."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.commands.dashboard import resolve_dashboard_host
from archon_horizon.commands.run import RunCommand
from archon_horizon.runlog import RunLogTree
from archon_horizon.server.app import create_server, create_server_with_fallback, dashboard_url
from archon_horizon.server.service import WorkspaceService
from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.sink import JsonlTranscriptSink


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    main(["--root", str(ws), "init", "--no-interactive"])
    return ws


def test_service_state_and_inbox_edit(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)

    assert service.state()["workspace"] == "ws"
    assert service.state()["local_inbox"] == []

    created = service.edit_inbox(
        "add", kind="hint", title="affine first", comment="start here", author="ground"
    )
    assert created["created"] == "I-0001"
    assert len(service.state()["local_inbox"]) == 1

    service.edit_inbox("complete", id="I-0001", author="ground")
    item = service.state()["local_inbox"][0]
    assert item["status"] == "closed"
    # the close transition is recorded in the item's history timeline
    hist = item["metadata"]["history"]
    assert any(h["field"] == "status" and h["to"] == "closed" and h["actor"] == "ground" for h in hist)

    # roadmap items take an author, comments, and history
    service.edit_roadmap("add", id="R-1", title="Affine", projects=["ag-main"], author="ground")
    with pytest.raises(ValueError):
        service.edit_roadmap("add", id="R-2", title="No author", projects=["ag-main"])
    service.edit_roadmap("comment", id="R-1", body="- progress note", author="horizon")
    service.edit_roadmap("status", id="R-1", status="done", author="ground")
    ritem = next(i for i in service.state()["roadmap"]["items"] if i["id"] == "R-1")
    assert ritem["metadata"]["comments"][0]["body"] == "- progress note"
    assert any(h["field"] == "status" and h["to"] == "done" for h in ritem["metadata"]["history"])

    service.edit_task(
        "add",
        id="prove-affine",
        title="Prove affine case",
        explanation="Use the roadmap item.",
        projects=["ag-main"],
        roadmap_refs=["affine-roadmap"],
        files=["projects/ag-main/Foo.lean"],
        author="ground",
    )
    task = service.state()["tasks"][0]
    assert task["id"] == "prove-affine"
    assert task["title"] == "Prove affine case"
    assert task["roadmap_refs"] == ["affine-roadmap"]
    assert task["metadata"]["author"] == "ground"

    # author is compulsory on add
    with pytest.raises(ValueError):
        service.edit_task("add", id="no-author", title="x", projects=["ag-main"])

    # comments + history land on tasks and surface in state
    service.edit_task("comment", id="prove-affine", body="**Started** the proof.", author="horizon")
    service.edit_task("status", id="prove-affine", status="running", author="horizon")
    task = next(t for t in service.state()["tasks"] if t["id"] == "prove-affine")
    assert task["metadata"]["comments"][0]["body"] == "**Started** the proof."
    fields = {h["field"] for h in task["metadata"]["history"]}
    assert {"created", "status"} <= fields


def test_task_done_records_history_and_syncs_roadmap(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)

    service.edit_roadmap("add", id="R-sync", title="Sync target", projects=["ag-main"], author="ground")
    service.edit_task(
        "add",
        id="sync-task",
        title="Sync task",
        explanation="Close the linked roadmap item.",
        projects=["ag-main"],
        roadmap_refs=["R-sync"],
        author="human",
    )

    service.edit_task("status", id="sync-task", status="running", author="human")
    service.edit_task("status", id="sync-task", status="done", author="human")

    task = next(t for t in service.state()["tasks"] if t["id"] == "sync-task")
    assert any(
        h["field"] == "status" and h["from"] == "running" and h["to"] == "done"
        for h in task["metadata"]["history"]
    )

    item = next(i for i in service.state()["roadmap"]["items"] if i["id"] == "R-sync")
    assert item["status"] == "done"
    assert any(
        h["field"] == "status" and h["from"] == "active" and h["to"] == "done"
        for h in item["metadata"]["history"]
    )
    assert any("sync-task" in c["body"] and "`done`" in c["body"] for c in item["metadata"]["comments"])


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
        assert json.loads(resp.read())["workspace"] == "ws"

        body = json.dumps(
            {"action": "add", "kind": "hint", "title": "x", "comment": "why", "author": "ground"}
        )
        conn.request("POST", "/api/inbox", body, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["created"] == "I-0001"

        conn.request("POST", "/api/blueprint/sync", "{}", {"Content-Type": "application/json"})
        resp = conn.getresponse()
        assert resp.status == 200
        assert json.loads(resp.read())["ok"] is True

        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        assert b"<!doctype html>" in resp.read()
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_public_host_resolution() -> None:
    assert resolve_dashboard_host("127.0.0.1") == "127.0.0.1"
    assert resolve_dashboard_host("0.0.0.0") == "0.0.0.0"
    assert resolve_dashboard_host("127.0.0.1", public=True) == "0.0.0.0"
    assert resolve_dashboard_host("0.0.0.0", public=True) == "0.0.0.0"
    with pytest.raises(ValueError):
        resolve_dashboard_host("192.168.1.10", public=True)


def test_dashboard_url_uses_browser_reachable_hosts() -> None:
    assert dashboard_url("127.0.0.1", 8765) == "http://127.0.0.1:8765"
    assert dashboard_url("0.0.0.0", 8765) == "http://localhost:8765"
    assert dashboard_url("::", 8765) == "http://[::1]:8765"
    assert dashboard_url("::1", 8765) == "http://[::1]:8765"


def test_run_command_starts_dashboard_server_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []

    class FakeServer:
        server_address = ("127.0.0.1", 8770)

        def shutdown(self) -> None:
            calls.append(("shutdown", None))

    def fake_create(root: Path, host: str, port: int, dist_dir: Path | None):
        calls.append(("create", (root, host, port, dist_dir)))
        return FakeServer()

    def fake_serve_server(**kwargs: object) -> None:
        calls.append(("serve", kwargs))

    monkeypatch.setattr("archon_horizon.server.app.create_server_with_fallback", fake_create)
    monkeypatch.setattr("archon_horizon.server.app.serve_server", fake_serve_server)

    command = RunCommand(tmp_path, dashboard_host="0.0.0.0", dashboard_port=8770)
    with command._dashboard_server():
        pass

    assert calls[0][0] == "create"
    assert calls[0][1][:3] == (tmp_path, "0.0.0.0", 8770)
    assert any(kind == "serve" for kind, _ in calls)
    assert ("shutdown", None) in calls


def test_run_command_can_disable_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_create(*args: object, **kwargs: object):
        raise AssertionError("dashboard should not start")

    monkeypatch.setattr("archon_horizon.server.app.create_server_with_fallback", fail_create)

    command = RunCommand(tmp_path, dashboard=False)
    with command._dashboard_server():
        pass


def test_server_tries_next_port_when_requested_port_is_busy(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    blocker = socket.socket()
    busy_port = None
    for candidate in range(20000, 40000):
        try:
            blocker.bind(("127.0.0.1", candidate))
            busy_port = candidate
            break
        except OSError:
            continue
    assert busy_port is not None
    blocker.listen(1)
    try:
        server = create_server_with_fallback(ws, "127.0.0.1", busy_port)
        try:
            assert server.server_address[1] != busy_port
            assert busy_port < server.server_address[1] <= busy_port + 25
        finally:
            server.server_close()
    finally:
        blocker.close()


def test_service_backfills_inline_subagents_into_session_tree(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    session = run.new_session("horizon")
    sink = JsonlTranscriptSink(session.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(
        TranscriptKind.TEXT,
        text="subagent report",
        data={"parent_tool_use_id": "toolu_abc", "subagent_type": "general-purpose"},
    ))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": True}))

    state = WorkspaceService(ws).state()

    child = state["runs"][0]["sessions"][0]["children"][0]
    assert child["session"] == "0001-general-purpose"
    assert child["meta"]["role"] == "subagent"
    assert child["meta"]["name"] == "general-purpose"


def test_service_report_returns_report_and_recommendation(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    session = run.new_session("ground")
    (session.path / "report.md").write_text("# Report\n\nFinal report.\n", "utf-8")
    (session.path / "recommendation.md").write_text("# Recommendation\n\nNext agent plan.\n", "utf-8")
    ref = session.transcript_path.relative_to(ws).as_posix()

    payload = WorkspaceService(ws).report(ref)

    assert payload["markdown"] == "# Report\n\nFinal report.\n"
    assert payload["recommendation"] == "# Recommendation\n\nNext agent plan.\n"


def test_stale_running_session_reads_interrupted_and_surfaces_model(tmp_path: Path) -> None:
    """A session left "running" (no session_end) by a dead run shows as
    interrupted — not a perpetual spinner — once its activity is stale and no
    live process holds the run lock. The observed model is surfaced for the
    sidebar chip."""
    from datetime import timedelta
    from archon_horizon.core.clock import utc_now

    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    session = run.new_session("horizon")
    sink = JsonlTranscriptSink(session.transcript_path)
    old = utc_now() - timedelta(hours=1)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, at=old, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_META, at=old, data={"model": "claude-opus-4-8"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, at=old, text="working…"))
    # No SESSION_END and no live run.lock -> the run died early.

    run_state = WorkspaceService(ws).state()["runs"][0]
    assert run_state["status"] == "interrupted"
    sess = run_state["sessions"][0]
    assert sess["status"] == "interrupted"
    assert sess["model"] == "claude-opus-4-8"


def test_recently_active_running_session_stays_running(tmp_path: Path) -> None:
    """A session still emitting activity is genuinely active even with no lock
    (covers concurrent runs the single-owner run lock can't see)."""
    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    session = run.new_session("horizon")
    sink = JsonlTranscriptSink(session.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, text="just now"))

    assert WorkspaceService(ws).state()["runs"][0]["sessions"][0]["status"] == "running"


def test_stopped_run_marks_recent_running_session_interrupted(tmp_path: Path) -> None:
    """A terminal `run.stopped` event ends the run: even a session that emitted
    activity moments ago (so the recency check would otherwise keep it active) is
    interrupted, not a perpetual spinner."""
    from archon_horizon.core.events import Event

    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    session = run.new_session("ground")
    sink = JsonlTranscriptSink(session.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "ground"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, text="just now"))  # recent, no SESSION_END

    service = WorkspaceService(ws)
    assert service.state()["runs"][0]["sessions"][0]["status"] == "running"

    service.stores.events.append(
        Event(type="run.stopped", id="e1", data={"run_id": run.id, "reason": "no-runnable-tasks", "round": 1})
    )
    run_state = WorkspaceService(ws).state()["runs"][0]
    assert run_state["status"] == "interrupted"
    assert run_state["sessions"][0]["status"] == "interrupted"


def test_run_status_follows_latest_agentic_session_after_resume(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()

    failed = run.new_session("horizon-R-1")
    sink = JsonlTranscriptSink(failed.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.ERROR, text="first attempt failed"))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": False}))

    resumed = run.new_session("horizon-R-1")
    sink = JsonlTranscriptSink(resumed.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, text="resume fixed it"))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": True}))

    run_state = WorkspaceService(ws).state()["runs"][0]

    assert [session["status"] for session in run_state["sessions"]] == ["failed", "completed"]
    assert run_state["status"] == "completed"


def test_run_status_ignores_trailing_system_session(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()

    failed = run.new_session("horizon-R-1")
    sink = JsonlTranscriptSink(failed.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": False}))

    system = run.new_session("system")
    system.write_meta({"role": "system"})
    sink = JsonlTranscriptSink(system.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": "system"}))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": True}))

    run_state = WorkspaceService(ws).state()["runs"][0]

    assert [session["status"] for session in run_state["sessions"]] == ["failed", "completed"]
    assert run_state["status"] == "failed"
