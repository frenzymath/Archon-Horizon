"""Live server: the service layer and the HTTP endpoints."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from archon_horizon.cli import main
from archon_horizon.commands.dashboard import resolve_dashboard_host
from archon_horizon.commands.run import RunCommand
from archon_horizon.runlog import RunLogTree
from archon_horizon.server.app import (
    create_server,
    create_server_with_fallback,
    dashboard_policy_rows,
    dashboard_url,
)
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

    conversation = service.edit_inbox(
        "add", kind="hint", title="coordinate", comment="split the proof",
        author="human", audience="task:T-1, task:T-2", conversation=True,
    )
    thread = next(
        item for item in service.state()["local_inbox"]
        if item["id"] == conversation["created"]
    )
    assert thread["author"] == "human"
    assert thread["audience"] == "task:T-1, task:T-2"
    assert thread["kind"] == "conversation"
    assert thread["metadata"]["conversation"] is True
    assert thread["metadata"]["participants"] == ["task:T-1", "task:T-2", "human"]
    assert thread["metadata"]["started_by"] == "human"

    # A team reply makes the human initiator unread; expanding the dashboard
    # thread calls the read action and acknowledges it again.
    service.local.add_comment(
        conversation["created"], "Team reply", author="horizon",
        metadata={"provenance": {"task": "T-1", "run": "0002"}},
    )
    thread = next(
        item for item in service.state()["local_inbox"]
        if item["id"] == conversation["created"]
    )
    assert thread["metadata"]["read_by"] == ["T-1"]
    service.edit_inbox("read", id=conversation["created"], author="human", reader="human")
    thread = next(
        item for item in service.state()["local_inbox"]
        if item["id"] == conversation["created"]
    )
    assert set(thread["metadata"]["read_by"]) == {"T-1", "human"}

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


def test_project_overview_index_does_not_scan_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)
    project = ws / "demo"
    project.mkdir()
    (project / "Demo.lean").write_text("theorem demo : True := by\n  sorry\n", encoding="utf-8")
    monkeypatch.setattr(service, "_discover_projects", lambda: ["demo"])
    monkeypatch.setattr(service, "_project_path", lambda name: project)

    # The table shell is configuration-only; source scanning happens through a
    # separate row endpoint so a large project cannot delay every row.
    monkeypatch.setattr("archon_horizon.server.service.list_lean_files", lambda path: (_ for _ in ()).throw(AssertionError("source scan")))
    assert service.serve_endpoint("/api/projects") == {
        "projects": [{"name": "demo", "depends_on": []}],
    }

    monkeypatch.undo()
    service = WorkspaceService(ws)
    monkeypatch.setattr(service, "_discover_projects", lambda: ["demo"])
    monkeypatch.setattr(service, "_project_path", lambda name: project)
    assert service.serve_endpoint("/api/project/metrics?project=demo") == {
        "name": "demo",
        "lean_files": 1,
        "loc": 2,
        "loc_code": 2,
        "sorries": 1,
    }


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


def test_dashboard_roadmap_crud_and_hierarchy_validation(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)

    service.edit_roadmap(
        "add",
        id="R-parent",
        title="Parent goal",
        projects=["ag-main"],
        author="ground",
    )
    created = service.edit_roadmap(
        "add",
        id="R-child",
        title="Child goal",
        summary="Initial plan",
        projects=["ag-main", "shared"],
        author="human",
        status="pending",
        kind="blueprint",
        priority="high",
        parent="R-parent",
        owner="proof-team",
        milestone="w4-gate",
        depends_on=["R-prereq"],
        inbox_refs=["I-0001"],
        task_refs=["prove-child"],
        pinned_commits=["abc123"],
    )
    assert created == {"ok": True, "id": "R-child"}
    item = next(i for i in service.state()["roadmap"]["items"] if i["id"] == "R-child")
    assert item["projects"] == ["ag-main", "shared"]
    assert item["status"] == "pending"
    assert item["kind"] == "blueprint"
    assert item["priority"] == "high"
    assert item["depends_on"] == ["R-prereq"]
    assert item["inbox_refs"] == ["I-0001"]
    assert item["task_refs"] == ["prove-child"]
    assert item["metadata"]["parent"] == "R-parent"
    assert item["metadata"]["owner"] == "proof-team"
    assert item["metadata"]["milestone"] == "w4-gate"
    assert item["metadata"]["pinned_commits"] == ["abc123"]

    service.edit_roadmap(
        "edit",
        id="R-child",
        title="Revised child",
        projects=["shared"],
        author="human",
        status="active",
        kind="proof",
        priority="urgent",
        parent="",
        owner="",
        milestone="",
        depends_on=[],
        inbox_refs=[],
        task_refs=[],
        pinned_commits=[],
    )
    item = next(i for i in service.state()["roadmap"]["items"] if i["id"] == "R-child")
    assert item["title"] == "Revised child"
    assert item["projects"] == ["shared"]
    assert item["status"] == "active"
    assert item["priority"] == "urgent"
    assert "parent" not in item["metadata"]
    assert "owner" not in item["metadata"]
    assert "milestone" not in item["metadata"]
    assert "pinned_commits" not in item["metadata"]
    assert item["metadata"]["author"] == "human"

    with pytest.raises(ValueError, match="already exists"):
        service.edit_roadmap(
            "add", id="R-child", title="Duplicate", projects=["shared"], author="human"
        )
    with pytest.raises(ValueError, match="item id"):
        service.edit_roadmap(
            "add", id="../outside", title="Unsafe", projects=["shared"], author="human"
        )
    with pytest.raises(ValueError, match="does not exist"):
        service.edit_roadmap("edit", id="R-child", parent="R-missing", author="human")
    service.edit_roadmap("edit", id="R-child", parent="R-parent", author="human")
    with pytest.raises(ValueError, match="cycle"):
        service.edit_roadmap("edit", id="R-parent", parent="R-child", author="human")

    service.edit_roadmap("delete", id="R-child", author="human")
    assert all(i["id"] != "R-child" for i in service.state()["roadmap"]["items"])


def test_dashboard_roadmap_generated_ids_fill_gaps(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)
    for item_id in ("R-0001", "R-0003"):
        service.edit_roadmap(
            "add", id=item_id, title=item_id, projects=["ag-main"], author="human"
        )
    assert service.edit_roadmap(
        "add", title="Generated", projects=["ag-main"], author="human"
    )["id"] == "R-0002"


def test_roadmap_activity_rolls_up_linked_tasks_and_children(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    service = WorkspaceService(ws)
    service.edit_roadmap(
        "add", id="R-parent", title="Parent", projects=["ag-main"], author="human"
    )
    service.edit_roadmap(
        "add", id="R-child", title="Child", projects=["ag-main"],
        parent="R-parent", author="human",
    )
    service.edit_task(
        "add", id="child-task", title="Child task", projects=["ag-main"],
        roadmap_refs=["R-child"], author="human",
    )
    service.edit_task(
        "comment", id="child-task", body="The task has new evidence.", author="horizon"
    )

    state = service.state()
    task = next(row for row in state["tasks"] if row["id"] == "child-task")
    task_comment_at = task["metadata"]["comments"][-1]["at"]
    child = next(row for row in state["roadmap"]["items"] if row["id"] == "R-child")
    parent = next(row for row in state["roadmap"]["items"] if row["id"] == "R-parent")

    assert child["activity"]["updated_at"] == task_comment_at
    assert child["activity"]["task_count"] == 1
    assert child["activity"]["updated_from_tasks"] is True
    assert parent["activity"]["updated_at"] == task_comment_at
    assert parent["activity"]["task_count"] == 1
    assert parent["activity"]["updated_from_descendants"] is True


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


def test_mutating_endpoints_refuse_cross_origin_writes(tmp_path: Path) -> None:
    """The POST endpoints mutate the workspace with no credentials, so a foreign
    Origin must be refused.

    A ``text/plain`` body is a CORS "simple request": a browser sends it
    cross-origin with no preflight, so without this guard any page the user has
    open can write to the inbox — which agents then read as instructions.
    """
    ws = _workspace(tmp_path)
    server = create_server(ws, "127.0.0.1", 0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def post_inbox(title: str, origin: str | None) -> int:
        conn = http.client.HTTPConnection("127.0.0.1", port)
        headers = {"Content-Type": "text/plain"}
        if origin is not None:
            headers["Origin"] = origin
        body = json.dumps(
            {"action": "add", "kind": "hint", "title": title, "comment": "why", "author": "ground"}
        )
        conn.request("POST", "/api/inbox", body, headers)
        return conn.getresponse().status

    try:
        assert post_inbox("evil", "https://evil.example") == 403
        assert post_inbox("wrong-port", "http://127.0.0.1:1") == 403
        assert post_inbox("opaque", "null") == 403
        # The dashboard's own fetches, and non-browser clients, still work.
        assert post_inbox("same-origin", f"http://127.0.0.1:{port}") == 200
        assert post_inbox("no-origin", None) == 200

        service = WorkspaceService(ws)
        # The API's `title` becomes the first line of the item body.
        titles = {item["body"].splitlines()[0] for item in service.state()["local_inbox"]}
        assert titles == {"same-origin", "no-origin"}  # no refused write landed
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


def test_dashboard_policy_rows_show_all_configured_freezes(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(
        """workspace:
  name: policy-test
  scheduler: {max_parallel_sessions: 3}
projects:
  demo:
    path: projects/demo
    freeze:
      files: [Demo/Frozen.lean]
      declarations: [Demo.fixed]
freeze:
  agents: [horizon]
  projects: [LeeSmooth]
  files: [Shared/*.lean]
  blueprint_nodes: [thm:locked]
""",
        "utf-8",
    )
    rows = dashboard_policy_rows(WorkspaceService(ws))
    assert rows == {
        "Frozen projects": "LeeSmooth",
        "Other freezes": "agents: horizon · files: 2 · declarations: 1 · blueprint nodes: 1",
        "Max parallel": "3",
    }


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


def test_live_process_marker_keeps_quiet_interactive_session_running(tmp_path: Path) -> None:
    """A human can leave a bare TUI quiet for more than the recency window; its
    live process marker is authoritative until that TUI exits."""
    import json
    import os
    import socket
    from datetime import timedelta
    from archon_horizon.core.clock import utc_now

    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    session = run.new_session("horizon-interactive")
    sink = JsonlTranscriptSink(session.transcript_path)
    old = utc_now() - timedelta(hours=1)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, at=old, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, at=old, text="waiting for the human"))
    (run.path / "process.json").write_text(json.dumps({
        "pid": os.getpid(), "host": socket.gethostname(), "started_at": old.isoformat(),
    }), "utf-8")

    state = WorkspaceService(ws).state()["runs"][0]
    assert state["status"] == "running"
    assert state["sessions"][0]["status"] == "running"


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


def test_run_status_activity_comes_from_latest_agentic_session(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    run = RunLogTree(ws / ".archon-horizon" / "runs").allocate()
    old = datetime.now(timezone.utc) - timedelta(minutes=10)
    recent = datetime.now(timezone.utc)

    horizon = run.new_session("horizon-R-1")
    horizon.write_meta({"role": "horizon"})
    sink = JsonlTranscriptSink(horizon.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, at=old, data={"role": "horizon"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, at=old, text="started but stale"))

    system = run.new_session("system")
    system.write_meta({"role": "system"})
    sink = JsonlTranscriptSink(system.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, at=recent, data={"role": "system"}))
    sink.emit(TranscriptEvent(TranscriptKind.TEXT, at=recent, text="recent deterministic work"))

    run_state = WorkspaceService(ws).state()["runs"][0]

    assert [session["status"] for session in run_state["sessions"]] == ["interrupted", "interrupted"]
    assert run_state["status"] == "interrupted"
