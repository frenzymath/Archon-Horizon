from datetime import datetime, timedelta, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import event, func, insert, select, update

from archon_horizon.pipeline.dashboard import dashboard_activity
from archon_horizon.pipeline.auth import Actor, issue_credential
from archon_horizon.pipeline.errors import DomainError
from archon_horizon.pipeline.persistence.records import create, get, json_value
from archon_horizon.pipeline.persistence.schema import tables
from test_pipeline_api import api, api_database, auth, mutate  # noqa: F401
from test_pipeline_provider_events import counter, observe, start
from test_pipeline_service import service_database, world  # noqa: F401


def test_original_run_directory_and_tree_use_actual_counts_and_usage(world):
    state = start(world)
    execution, thread, _ = state
    assignment = get(world.conn, "assignment", execution["assignment_id"])
    observe(world, state, counter(str(uuid4()), 12, 4))
    observe(world, state, {"type": "item.completed", "item": {"id": "spawn", "type": "collab_tool_call", "tool": "spawn_agent",
        "task_name": "check_definitions", "description": "Check reusable definitions and unnecessary assumptions.",
        "receiver_thread_ids": ["native-child"], "agents_states": {"native-child": {"status": "running"}}}})
    observe(world, state, {"type": "item.completed", "item": {"id": "wait", "type": "collab_tool_call", "tool": "wait",
        "agents_states": {"native-child": {"status": "completed", "usage": {"input_tokens": 30, "output_tokens": 9}}}}})
    page = dashboard_activity.runs(world.conn, world.actor, project_id=world.project["id"])
    run = next(row for row in page["runs"] if row["id"] == assignment["run_id"])
    assert run["usage"]["tokens_in"] == 42
    assert run["usage"]["cost_usd"] is None
    assert run["slot_capacity"] == 2
    assert run["native_subagent_count"] == 1
    detail = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service)
    parent = next(row for row in detail["sessions"] if row["id"] == assignment["id"])
    assert not any(row["native"] for row in detail["sessions"])
    assert run["total_session_count"] == run["session_count"]
    child, = parent["subagents"]
    assert child["title"] == "check_definitions"
    assert child["description"] == "Check reusable definitions and unnecessary assumptions."
    assert parent["usage"]["tokens_in"] == 12
    assert child["usage"]["tokens_in"] == 30
    assert child["parent_session_id"] == assignment["id"]
    assert child["status"] == "succeeded"
    assert child["attempt_count"] == 0  # Native children are not separate worker processes.
    session = dashboard_activity.session_detail(world.conn, world.actor, assignment["id"], store=world.service.store, service=world.service)
    assert session["attempts"][0]["id"] == execution["id"]
    assert session["reports"][0]["label"] == "Current goal ledger"
    assert "Current goal ledger" in session["reports"][0]["markdown"]
    assert not any(event["kind"].startswith("hook.") for event in session["events"])


@pytest.mark.parametrize("compact", [False, True])
def test_orchestrator_function_is_projected_as_its_dashboard_profile(world, compact):
    # Reconstruct a saved supervisor profile; new launches cannot create one.
    run = world.run(orchestrated=True)
    world.disable_automations(run)
    assignment = world.assignment(run, role="maintainer", functions=["orchestrator"])
    detail = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service, compact=compact)
    session = next(row for row in detail["sessions"] if row["id"] == assignment["id"])
    assert session["role"] == "orchestrator"
    assert session["functions"] == ["orchestrator"]


def test_subagent_uses_the_pinned_reviewer_description_not_current_catalog(world):
    from archon_horizon.pipeline.persistence.records import snapshot, change
    state = start(world)
    observe(world, state, {"type": "item.completed", "item": {"id": "spawn", "type": "collab_tool_call", "tool": "spawn_agent",
        "task_name": "hz_review_internal_identifier", "receiver_thread_ids": ["native-reviewer"]}})
    descriptor = create(world.conn, "reviewer_descriptor", project_id=world.project["id"], slug="custom-generalization",
                        instructions="Check whether this specific definition admits a useful generalization.",
                        functions=[], model_options={})
    pinned = snapshot(world.conn, "reviewer_descriptor", descriptor, world.actor.id)
    child = world.conn.execute(select(tables["provider_thread"]).where(tables["provider_thread"].c.kind == "child")).mappings().one()
    world.conn.execute(update(tables["provider_request"]).where(tables["provider_request"].c.provider_thread_id == child["id"])
        .values(status="completed"))
    create(world.conn, "provider_request", provider_thread_id=child["id"], execution_id=state[0]["id"],
           number=2, reason="review", status="running", reviewer_descriptor_id=descriptor["id"],
           reviewer_descriptor_revision_id=pinned)
    change(world.conn, "reviewer_descriptor", descriptor["id"], instructions="A later rubric, not used by this child.")
    session = dashboard_activity.session_detail(world.conn, world.actor, state[0]["assignment_id"],
        store=world.service.store, service=world.service)
    entry, = session["subagents"]
    assert entry["title"] == "custom-generalization"
    assert entry["description"] == descriptor["instructions"]
    assert entry["descriptor_revision"] == 1
    assert dashboard_activity.session_detail(world.conn, world.actor, child["id"],
        store=world.service.store, service=world.service)["title"] == "custom-generalization"


def test_original_activity_directory_paginates_newest_first_and_scopes_access(world):
    runs = [world.run() for _ in range(3)]
    page = dashboard_activity.runs(world.conn, world.actor, limit=2)
    assert [row["id"] for row in page["runs"]] == [runs[2]["id"], runs[1]["id"]]
    earlier = dashboard_activity.runs(world.conn, world.actor, before=page["next_before"], limit=2)
    assert [row["id"] for row in earlier["runs"]] == [runs[0]["id"]]
    outsider = create(world.conn, "principal", kind="human", username="outsider_" + uuid4().hex, display_name="Outsider")
    actor = Actor(outsider["id"], "human", {"username": outsider["username"]}, "api_key")
    assert dashboard_activity.runs(world.conn, actor)["runs"] == []
    with pytest.raises(DomainError):
        dashboard_activity.run_detail(world.conn, actor, runs[0]["id"], service=world.service)


def test_desktop_routes_use_v3_auth_and_current_records(api):
    client, database, world, run, token, _ = api
    directory = client.get("/api/v3/dashboard/activity/runs", headers=auth(token))
    assert directory.status_code == 200, directory.text
    assert any(row["id"] == str(run["id"]) for row in directory.json()["runs"])
    detail = client.get(f"/api/v3/dashboard/activity/runs/{run['id']}", headers=auth(token))
    assert detail.status_code == 200, detail.text
    session_id = detail.json()["sessions"][0]["id"]
    paths = [f"/api/v3/dashboard/activity/sessions/{session_id}",
             f"/api/v3/dashboard/activity/sessions/{session_id}?compact=true",
             f"/api/v3/dashboard/activity/sessions/{session_id}?view=reports",
             f"/api/v3/dashboard/activity/runs/{run['id']}?compact=true",
             f"/api/v3/dashboard/activity/runs/{run['id']}?view=metrics",
             f"/api/v3/dashboard/activity/runs/{run['id']}?view=queue",
             f"/api/v3/dashboard/activity/sessions/{session_id}/events?limit=2",
             f"/api/v3/dashboard/activity/sessions/{session_id}/events?limit=2&compact=true",
             f"/api/v3/dashboard/activity/sessions/{session_id}/logs?limit=2"]
    for path in paths:
        assert client.get(path).status_code == 401
        result = client.get(path, headers=auth(token))
        assert result.status_code == 200, (path, result.text)
        assert "credential_ref" not in result.text
    with database.transaction() as conn:
        outsider = create(conn, "principal", kind="human", username="outsider_" + uuid4().hex, display_name="Outsider")
        _, other_token = issue_credential(conn, outsider["id"], "api_key", "No project access")
    for path in paths:
        assert client.get(path, headers=auth(other_token)).status_code == 403


def test_native_child_events_do_not_include_unrelated_assignment_activity(world):
    state = start(world)
    execution, _, _ = state
    observe(world, state, {"type": "item.completed", "item": {"id": "spawn", "type": "collab_tool_call", "tool": "spawn_agent",
        "receiver_thread_ids": ["native-child"]}})
    child = world.conn.execute(select(tables["provider_thread"]).where(tables["provider_thread"].c.kind == "child")).mappings().one()
    create(world.conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"], kind="progress",
           summary="Parent-only progress", occurred_at=datetime.now(timezone.utc))
    events = dashboard_activity.events(world.conn, world.actor, child["id"], store=world.service.store)
    assert not any(row["title"] == "Parent-only progress" for row in events["events"])


def test_desktop_run_commands_keep_revision_and_project_permission_checks(api):
    client, database, world, run, token, _ = api
    detail = client.get(f"/api/v3/dashboard/activity/runs/{run['id']}", headers=auth(token)).json()
    body = {"operation": "cancel_run", "target_id": detail["id"], "expected_revision": detail["revision"], "args": {}}
    with database.transaction() as conn:
        viewer = create(conn, "principal", kind="human", username="viewer_" + uuid4().hex, display_name="Viewer")
        conn.execute(insert(tables["project_grant"]).values(principal_id=viewer["id"], project_id=world.project["id"], role="viewer"))
        _, viewer_token = issue_credential(conn, viewer["id"], "api_key", "Viewer")
    assert mutate(client, "/api/v3/commands", body, viewer_token).status_code == 403
    stale = mutate(client, "/api/v3/commands", {**body, "expected_revision": detail["revision"] + 1}, token)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"
    key = str(uuid4())
    result = mutate(client, "/api/v3/commands", body, token, key)
    assert result.status_code == 200, result.text
    replay = mutate(client, "/api/v3/commands", body, token, key)
    assert replay.status_code == 200 and replay.json() == result.json()


def test_provider_output_maps_to_disclosure_not_visible_summary():
    row = {"id": "event", "kind": "provider.observed", "category": "tool", "title": "Read Main.lean",
           "detail": "command: cat Main.lean\noutput: theorem example", "occurred_at": datetime.now(timezone.utc)}
    event = dashboard_activity._event(row, {"id": "assignment", "run_id": "run"})
    assert event["data"]["detail"] == row["detail"]
    assert "summary" not in event["data"]


def test_queue_conditions_are_readable_and_match_scheduler_readiness(world):
    run = world.run()
    world.disable_automations(run)
    dependency = world.assignment(run)
    waiting = world.assignment(run, start_condition={"version": 1, "expression": {"op": "all", "args": [
        {"op": "queue_below", "run_id": str(run["id"]), "count": 2},
        {"op": "status_in", "target": {"kind": "assignment", "id": str(dependency["id"])},
         "values": ["completed", "failed", "cancelled"]}]}})
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == waiting["id"]).values(queue_rank=-1))
    detail = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service)
    session = next(row for row in detail["sessions"] if row["id"] == waiting["id"])
    admission = session["context"]["admission"]
    assert session["queue_position"] == 1
    assert admission["state"] == "waiting"
    assert f"Session #{dependency['number']} is completed or failed or cancelled" in admission["summary"]
    assert "Fewer than 2 ready worker sessions" in admission["summary"]
    assert str(dependency["id"]) not in admission["summary"]
    assert "condition" not in admission  # Structured internals are not dashboard presentation.
    queue = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service, view="queue")
    assert queue["admissions"][str(waiting["id"])] == admission
    world.conn.execute(update(tables["assignment"]).where(tables["assignment"].c.id == dependency["id"]).values(status="completed"))
    detail = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service)
    ready = next(row for row in detail["sessions"] if row["id"] == waiting["id"])["context"]["admission"]
    assert ready["state"] == "ready"


def test_queue_readiness_includes_backoff_deadlines_and_stale_forge(world):
    run = world.run()
    world.disable_automations(run)
    now = world.conn.execute(select(func.now())).scalar_one()
    deferred = world.assignment(run, not_before=now + timedelta(hours=1), expires_at=now + timedelta(hours=2))
    stale = world.assignment(run, start_condition={"version": 1, "expression": {
        "op": "forge_open_count", "project_id": str(world.project["id"]), "repository_ids": [],
        "kinds": ["pull_request"], "labels": ["awaiting-review"], "match": "all", "at_least": 1}})
    detail = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service)
    admissions = {row["id"]: row["context"]["admission"] for row in detail["sessions"]}
    assert admissions[deferred["id"]]["state"] == "waiting"
    assert "Deferred until" in admissions[deferred["id"]]["reason"]
    assert admissions[deferred["id"]]["expires_at"] == deferred["expires_at"]
    assert admissions[stale["id"]]["state"] == "unknown"
    assert "stale" in admissions[stale["id"]]["reason"]
    assert "awaiting-review" in admissions[stale["id"]]["summary"]


def test_queue_readiness_reports_physical_slot_contention(world):
    run = world.run()
    world.disable_automations(run)
    for _ in range(2):
        world.assignment(run)
        assert world.claim() is not None
    waiting = world.assignment(run)
    detail = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service)
    admission = next(row for row in detail["sessions"] if row["id"] == waiting["id"])["context"]["admission"]
    assert admission["state"] == "waiting"
    assert "all 2 compatible execution slots are occupied" in admission["reason"]
    assert world.host["slug"] in admission["reason"]


def test_compact_run_does_not_load_provider_history_usage_or_evaluate_queue(world, monkeypatch):
    run = world.run()
    world.assignment(run)
    def heavy(*args, **kwargs):
        pytest.fail("Compact tree performed detail work")
    monkeypatch.setattr(dashboard_activity, "_sessions", heavy)
    monkeypatch.setattr(dashboard_activity, "admissions", heavy)
    queries = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)
    event.listen(world.conn, "before_cursor_execute", capture)
    try:
        result = dashboard_activity.run_detail(world.conn, world.actor, run["id"], service=world.service, compact=True)
        directory = dashboard_activity.runs(world.conn, world.actor, compact=True)
    finally:
        event.remove(world.conn, "before_cursor_execute", capture)
    assert result["sessions"] and directory["runs"]
    assert all("subagents" not in row and "attempts" not in row for row in result["sessions"])
    assert result["metrics_pending"] and result["usage"]["tokens_in"] is None
    assert len(json.dumps(json_value(result)).encode()) < 12000
    assert not any(name in query for query in queries for name in ("usage_record", "provider_request", "activity_artifact"))


def test_compact_session_and_report_view_do_not_read_events(world, monkeypatch):
    state = start(world)
    identifier = state[0]["assignment_id"]
    def heavy(*args, **kwargs):
        pytest.fail("Session metadata or report read the event history")
    monkeypatch.setattr(dashboard_activity, "events", heavy)
    result = dashboard_activity.session_detail(world.conn, world.actor, identifier,
        store=world.service.store, service=world.service, compact=True)
    assert "events" not in result and "reports" not in result
    monkeypatch.setattr(dashboard_activity, "_sessions", heavy)
    report = dashboard_activity.session_detail(world.conn, world.actor, identifier,
        store=world.service.store, service=world.service, view="reports")
    assert report["reports"][0]["label"] == "Current goal ledger"


def test_event_preview_has_scoped_lazy_full_body(world):
    from archon_horizon.pipeline.dashboard.activity_display import select_provider_event
    from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
    state = start(world)
    text = "Long public reasoning. " * 300
    execution, thread, request = state
    handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(), execution_id=execution["id"],
        epoch=execution["number"], kind="provider_observed", occurred_at=datetime.now(timezone.utc).timestamp(),
        payload={"event": "native_event", "request_id": str(request["id"]), "provider_thread_record_id": str(thread["id"]),
            "adapter": "codex", "raw": select_provider_event("codex", {"type": "item.completed", "item": {
                "id": "long-message", "type": "agent_message", "text": text}})}), world.service, world.scheduler)
    identifier = state[0]["assignment_id"]
    page = dashboard_activity.events(world.conn, world.actor, identifier, store=world.service.store, compact=True, limit=15)
    preview = next(row for row in page["events"] if row["kind"] == "agent.message")
    assert len(preview["data"]["markdown"].encode()) <= 1200
    assert preview["detail_path"].endswith(str(preview["id"]))
    full = dashboard_activity.event_detail(world.conn, world.actor, identifier, preview["id"], store=world.service.store)
    assert full["data"]["markdown"] == text
    other = world.assignment(get(world.conn, "run", get(world.conn, "assignment", identifier)["run_id"]))
    with pytest.raises(DomainError, match="Event not found"):
        dashboard_activity.event_detail(world.conn, world.actor, other["id"], preview["id"], store=world.service.store)
