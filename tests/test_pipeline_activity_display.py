from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

from archon_horizon.pipeline.dashboard.activity_display import display_event, select_provider_event, safe_text, skills_referenced
from archon_horizon.pipeline.dashboard.readmodels import assignment_activity
from archon_horizon.pipeline.persistence.records import create
from archon_horizon.pipeline.persistence.schema import tables
from archon_horizon.pipeline.worker.activity_replay import replay_once
from archon_horizon.pipeline.worker.journal import DurableJournal
from archon_horizon.pipeline.execution.worker_events import WorkerOperation, handle
from test_pipeline_provider_events import start
from test_pipeline_service import service_database, world  # noqa: F401


def test_display_messages_tools_are_bounded_and_idempotently_redacted():
    raw = {"type": "item.completed", "item": {"id": "tool-1", "type": "command_execution",
        "command": 'curl -H "Authorization: Bearer sensitive" https://user:password@example.org',
        "aggregated_output": json.dumps({"api_key": "private-key", "token": "reviewer-secret", "Authorization": "token another-secret"}) + "\n" + "x" * 20000,
        "exit_code": 1}}
    selected = select_provider_event("codex", raw)
    assert "sensitive" not in str(selected)
    assert "private-key" not in str(selected)
    assert "reviewer-secret" not in str(selected)
    assert "another-secret" not in str(selected)
    assert "user:password" not in str(selected)
    assert len(selected["horizon_activity"]["detail"]) < 12100
    assert select_provider_event("codex", selected) == selected
    assert "Exit code: 1" in selected["horizon_activity"]["detail"]
    assert display_event("codex", {"type": "item.completed", "item": {"type": "reasoning", "text": "not visible"}}) is None
    assert display_event("claude", {"type": "user", "message": {"content": [{"type": "text", "text": "user prompt"}]}}) is None
    assert display_event("claude", {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "not visible"}]}}) is None
    assert "my-secret" not in safe_text("tool --password my-secret")
    assert "my-secret" not in safe_text("Bearer my-secret")
    assert "my-secret" not in safe_text("-----BEGIN PRIVATE KEY-----\nmy-secret\n-----END PRIVATE KEY-----")


def test_display_claude_tool_output_and_visible_message():
    selected = select_provider_event("claude", {"type": "assistant", "message": {"id": "message-1", "content": [
        {"type": "text", "text": "I found the missing import."},
        {"type": "tool_use", "id": "call-1", "name": "Read", "input": {"file_path": "Proof.lean"}}]}})
    assert "I found the missing import." in selected["horizon_activity"]["detail"]
    assert "Proof.lean" in selected["horizon_activity"]["detail"]
    assert select_provider_event("claude", selected) == selected
    assert "hz_" not in safe_text("hz_" + "A" * 43)
    other = display_event("claude", {"type": "assistant", "message": {"id": "message-1", "content": [
        {"type": "text", "text": "The import is fixed."}]}})
    assert other["key"] != selected["horizon_activity"]["key"]


def test_skill_references_are_bounded_to_pinned_skill_paths():
    display = display_event("codex", {"type": "item.completed", "item": {
        "id": "skill-read", "type": "command_execution", "command":
            'cat "$HORIZON_SKILLS_DIR/operations/horizon-efficiency/SKILL.md"',
        "exit_code": 0, "aggregated_output":
            "The output mentions review/library-audit/SKILL.md but is not a command path.",
    }})
    assert skills_referenced(display) == ["horizon-efficiency"]


def test_provider_skill_reads_are_recorded_in_activity(world):
    execution, thread, request = start(world)
    observed = datetime.now(timezone.utc).timestamp()
    raw = {"type": "item.completed", "item": {"id": "skill-read", "type": "command_execution",
        "command": 'cat "$HORIZON_SKILLS_DIR/review/library-audit/SKILL.md"',
        "exit_code": 0, "aggregated_output": "library audit guidance"}}
    result = handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(), execution_id=execution["id"],
        epoch=execution["number"], kind="provider_observed", occurred_at=observed, payload={
            "event": "native_event", "request_id": str(request["id"]),
            "provider_thread_record_id": str(thread["id"]), "adapter": "codex",
            "raw": select_provider_event("codex", raw)}), world.service, world.scheduler)
    activity = world.conn.execute(select(tables["activity"]).where(tables["activity"].c.id == result["activity_id"])).mappings().one()
    assert activity["skills_used"] == ["library-audit"]


def test_activity_projection_detail_dedup_usage_and_api_timeline(world):
    execution, thread, request = start(world)
    raw = {"type": "item.completed", "item": {"id": "message-1", "type": "agent_message", "text": "Checking the theorem hypotheses."}}
    observed = datetime.now(timezone.utc).timestamp()
    def send(event):
        return handle(world.conn, world.host_actor, WorkerOperation(operation_id=uuid4(), execution_id=execution["id"],
            epoch=execution["number"], kind="provider_observed", occurred_at=observed, payload={
                "event": "native_event", "request_id": str(request["id"]), "provider_thread_record_id": str(thread["id"]),
                "adapter": "codex", "raw": select_provider_event("codex", event)}), world.service, world.scheduler)
    first = send(raw)
    assert send(raw)["activity_id"] == first["activity_id"]
    from test_pipeline_provider_events import counter
    send(counter(str(uuid4()), 500, 24))
    principal = world.conn.execute(select(tables["principal"]).where(tables["principal"].c.execution_id == execution["id"])).mappings().one()
    create(world.conn, "api_request", principal_id=principal["id"], project_id=world.project["id"],
           operation="forge.read", idempotency_key="read-1", request_sha256="0" * 64,
           status="completed", response={"private": "should not be included"}, expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    create(world.conn, "outbox_operation", actor_principal_id=principal["id"],
           project_id=world.project["id"], kind="forge_comment", idempotency_key="comment-1", schema_version=1,
           payload={"secret": "not displayed"})
    result = assignment_activity(world.conn, world.actor, execution["assignment_id"], None, 100, store=world.service.store)
    assert len(result["items"]) == 4
    message = next(row for row in result["items"] if row["category"] == "message")
    assert message["title"] == "Checking the theorem hypotheses."
    assert message["detail"] == "Checking the theorem hypotheses."
    assert next(row for row in result["items"] if row["category"] == "usage")["usage"]["input_tokens"] == 500
    assert {row["category"] for row in result["items"]} == {"message", "usage", "api", "forge"}
    assert "not displayed" not in str(result) and "should not be included" not in str(result)
    cursor, seen = None, []
    while True:
        page = assignment_activity(world.conn, world.actor, execution["assignment_id"], cursor, 1, store=world.service.store)
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 4


def test_replay_does_not_restart_provider_or_duplicate_accounting(tmp_path, monkeypatch):
    journal = DurableJournal(tmp_path)
    execution_id, request_id, thread_id, harness_id = (str(uuid4()) for _ in range(4))
    journal.grant_lease(execution_id, 1, 60)
    journal.checkpoint(execution_id, 1, {"harness_id": harness_id, "provider_thread_record_id": thread_id})
    journal.begin_request(request_id, execution_id, 1, "private request")
    directory = tmp_path / "requests" / request_id
    directory.mkdir(parents=True)
    line = json.dumps({"type": "item.completed", "item": {"id": "msg", "type": "agent_message", "text": "Checking declarations"}})
    (directory / "stdout.jsonl").write_text(line + "\n" + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 99}}) + "\n")
    config = SimpleNamespace(harnesses=[SimpleNamespace(id=harness_id, adapter="codex_exec")])
    offsets = sqlite3.connect(":memory:")
    offsets.execute("CREATE TABLE activity_cursor(request_id TEXT PRIMARY KEY, offset INTEGER NOT NULL)")
    observed_at = time.time() + 100
    monkeypatch.setattr("archon_horizon.pipeline.worker.contracts.time.time", lambda: observed_at)
    assert replay_once(config, journal, offsets) == 1
    assert replay_once(config, journal, offsets) == 0
    # Lost cursor after durable enqueue is also safe.
    offsets.execute("DELETE FROM activity_cursor")
    offsets.commit()
    assert replay_once(config, journal, offsets) == 0
    operation = journal.records()[0]
    envelope = json.loads(operation["envelope"])
    assert envelope["occurred_at"] == observed_at
    assert "at the displayed time" in envelope["payload"]["raw"]["horizon_activity"]["detail"]
    assert "input_tokens" not in operation["envelope"]
    assert "private request" not in operation["envelope"]
    assert journal.request(request_id)["state"] == "uncertain"
    # A later line advances activity time while a lost cursor keeps the first
    # observation's immutable timestamp and idempotency digest.
    with (directory / "stdout.jsonl").open("a") as stream:
        stream.write(json.dumps({"type": "item.completed", "item": {"id": "msg-2", "type": "agent_message", "text": "Next check"}}) + "\n")
    monkeypatch.setattr("archon_horizon.pipeline.worker.contracts.time.time", lambda: observed_at + 60)
    assert replay_once(config, journal, offsets) == 1
    assert [json.loads(row["envelope"])["occurred_at"] for row in journal.records()] == [observed_at, observed_at + 60]
    journal.close()


def test_replayed_messages_keep_log_order_across_page_boundaries(world):
    execution, _, _ = start(world)
    request_start = datetime.now(timezone.utc) - timedelta(hours=1)
    recorded = datetime.now(timezone.utc)
    for index in range(5):
        create(world.conn, "activity", assignment_id=execution["assignment_id"], execution_id=execution["id"],
               kind="progress", summary=f"Message {index}", occurred_at=request_start,
               created_at=recorded + timedelta(seconds=index))
    cursor, summaries = None, []
    while True:
        result = assignment_activity(world.conn, world.actor, execution["assignment_id"], cursor, 2)
        summaries.extend(row["summary"] for row in result["items"])
        assert all(row["occurred_at"] == request_start for row in result["items"])
        cursor = result["next_cursor"]
        if cursor is None:
            break
    assert summaries == [f"Message {index}" for index in reversed(range(5))]
