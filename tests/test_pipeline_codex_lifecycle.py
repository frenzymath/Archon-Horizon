from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from archon_horizon.pipeline.worker.codex_lifecycle import CodexLifecycleObserver, terminal_children


def record(payload, kind="response_item", at=None):
    return json.dumps({"type": kind, "timestamp": (at or datetime.now(timezone.utc)).isoformat(), "payload": payload}) + "\n"


def output(key="/root/reviewer", status="completed"):
    return {"type": "function_call_output", "call_id": "list-call", "output": json.dumps({
        "agents": [{"agent_name": key, "agent_status": {status: "Private assessment: do not retain"}}]})}


CALL = {"type": "function_call", "call_id": "list-call", "name": "list_agents", "arguments": "{}"}


@pytest.fixture
def observer(tmp_path):
    operations = {}
    def enqueue(operation):
        operations[operation.operation_id] = operation
        return True
    journal = SimpleNamespace(state_root=tmp_path / "journal", operation=operations.get, enqueue=enqueue)
    native = str(uuid4())
    source = tmp_path / "provider" / ".codex" / "sessions" / "2026" / "09" / "30" / ("rollout-date-" + native + ".jsonl")
    source.parent.mkdir(parents=True)
    source.write_text(record({"id": native}, "session_meta"))
    reader = CodexLifecycleObserver(journal, tmp_path / "provider", request_id=str(uuid4()),
        execution_id=str(uuid4()), epoch=1, thread_record_id=str(uuid4()), since=0)
    return reader, source, native, operations


def test_only_correlated_native_completion_is_selected_without_report_text():
    calls = {}
    assert terminal_children(output(), calls) == []
    assert terminal_children(CALL, calls) == []
    assert terminal_children(output(), calls) == [{"key": "/root/reviewer", "status": "completed"}]
    assert terminal_children(output(), calls) == []
    assert terminal_children({**CALL, "name": "exec"}, calls) == []
    assert terminal_children(output(), calls) == []
    terminal_children({**CALL, "name": "collaboration.wait_agent"}, calls)
    assert terminal_children({"type": "function_call_output", "call_id": "list-call",
        "output": '{"status":{"child":{"failed":"Private failure"}}}'}, calls) == [{"key": "child", "status": "failed"}]


def test_rollout_completion_without_stdout_is_durable_bounded_and_resumable(observer):
    reader, source, native, operations = observer
    with source.open("a") as stream:
        stream.write("malformed json\n" + record(CALL) + record(output()))
    assert reader.poll(native, force=True) == 1
    operation, = operations.values()
    assert operation.payload["raw"] == {"type": "horizon.child_notifications",
        "children": [{"key": "/root/reviewer", "status": "completed"}]}
    assert "Private" not in json.dumps(operation.payload)
    assert not reader.poll(native, force=True)
    restored = CodexLifecycleObserver(reader.journal, reader.root.parent.parent, request_id=reader.request_id,
        execution_id=reader.execution_id, epoch=1, thread_record_id=reader.thread_record_id, since=0)
    assert not restored.poll(native, force=True)
    assert len(operations) == 1


def test_rollout_rejects_other_thread_metadata_symlink_and_old_events(observer):
    reader, source, native, operations = observer
    source.write_text(record({"id": str(uuid4())}, "session_meta") + record(CALL) + record(output()))
    assert not reader.poll(native, force=True)
    source.write_text(record({"id": native}, "session_meta") + record(CALL) + record(output()))
    reader.since = datetime.now(timezone.utc).timestamp() + 60
    assert not reader.poll(native, force=True)
    assert not operations
    assert not reader.poll(str(uuid4()), force=True)
    source.unlink()
    source.symlink_to(Path(__file__))
    assert not reader.poll(native, force=True)


def test_rollout_partial_and_oversized_lines_do_not_hide_later_completion(observer):
    reader, source, native, operations = observer
    with source.open("a") as stream:
        stream.write("x" * (1024 * 1024 + 5) + "\n" + record(CALL) + record(output())[:-1])
    assert not reader.poll(native, force=True)
    with source.open("a") as stream:
        stream.write("\n")
    assert reader.poll(native, force=True) == 1
    assert len(operations) == 1


def test_cursor_advances_only_after_journal_enqueue(observer):
    reader, source, native, operations = observer
    with source.open("a") as stream:
        stream.write(record(CALL) + record(output()))
    enqueue = reader.journal.enqueue
    def fail(operation):
        raise RuntimeError("journal full")
    reader.journal.enqueue = fail
    with pytest.raises(RuntimeError, match="journal full"):
        reader.poll(native, force=True)
    reader.journal.enqueue = enqueue
    assert reader.poll(native, force=True) == 1
    assert len(operations) == 1


@pytest.mark.parametrize("saved", ["null", "[]", "{", '{"offset":"bad","calls":{}}', '{"offset":0,"calls":[]}'])
def test_corrupt_cursor_is_rebuilt_without_losing_lifecycle(observer, saved):
    reader, source, native, operations = observer
    reader.cursor_path.parent.mkdir(parents=True)
    reader.cursor_path.write_text(saved)
    restored = CodexLifecycleObserver(reader.journal, reader.root.parent.parent, request_id=reader.request_id,
        execution_id=reader.execution_id, epoch=1, thread_record_id=reader.thread_record_id, since=0)
    with source.open("a") as stream:
        stream.write(record(CALL) + record(output()))
    assert restored.poll(native, force=True) == 1


def test_rollout_filesystem_failure_does_not_interrupt_provider(observer, monkeypatch):
    reader, source, native, operations = observer
    def inaccessible(*args):
        raise PermissionError("rollout unavailable")
    monkeypatch.setattr(reader, "_find", inaccessible)
    assert reader.poll(native, force=True) == 0


@pytest.mark.parametrize("header", ["[]", '{"type":"session_meta","payload":[]}'])
def test_malformed_rollout_metadata_does_not_interrupt_provider(observer, header):
    reader, source, native, operations = observer
    source.write_text(header + "\n" + record(CALL) + record(output()))
    assert reader.poll(native, force=True) == 0


def test_new_turn_starts_at_existing_rollout_boundary(observer):
    reader, source, native, operations = observer
    with source.open("a") as stream:
        stream.write(record(CALL) + record(output("/root/old")))
    resumed = CodexLifecycleObserver(reader.journal, reader.root.parent.parent, request_id=reader.request_id,
        execution_id=reader.execution_id, epoch=1, thread_record_id=reader.thread_record_id, since=0,
        native_thread_id=native)
    with source.open("a") as stream:
        stream.write(record(CALL) + record(output("/root/current")))
    assert resumed.poll(native, force=True) == 1
    operation, = operations.values()
    assert operation.payload["raw"]["children"] == [{"key": "/root/current", "status": "completed"}]


def final_answer(author=None):
    author = author or "/root/hz_review_" + uuid4().hex
    return {"type": "agent_message", "author": author, "recipient": "/root", "content": [{
        "type": "input_text", "text": f"Message Type: FINAL_ANSWER\nTask name: /root\nSender: {author}\nPayload:\nPrivate findings"}]}


def test_provider_delivered_final_answer_settles_without_parent_polling(observer):
    reader, source, native, operations = observer
    payload = final_answer()
    with source.open("a") as stream:
        stream.write(record(payload))
    assert reader.poll(native, force=True) == 1
    operation, = operations.values()
    assert operation.payload["raw"]["children"] == [{"key": payload["author"], "status": "completed", "discovered": True}]
    assert "Private" not in json.dumps(operation.payload)


@pytest.mark.parametrize("case", ["ordinary_message", "quoted_header", "wrong_recipient", "wrong_sender", "wrong_task",
                                  "user_message", "missing_content", "text_block", "bad_path"])
def test_only_provider_final_envelope_counts(case):
    payload = final_answer()
    if case == "ordinary_message":
        payload["content"][0]["text"] = payload["content"][0]["text"].replace("FINAL_ANSWER", "MESSAGE")
    elif case == "quoted_header":
        payload["content"][0]["text"] = "Here is a quoted final answer:\n" + payload["content"][0]["text"]
    elif case == "wrong_recipient":
        payload["recipient"] = "/root/another"
    elif case == "wrong_sender":
        payload["author"] = "/root/hz_review_" + uuid4().hex
    elif case == "wrong_task":
        payload["content"][0]["text"] = payload["content"][0]["text"].replace("Task name: /root", "Task name: /other")
    elif case == "user_message":
        payload["type"] = "message"
        payload["role"] = "user"
    elif case == "missing_content":
        payload["content"] = []
    elif case == "text_block":
        payload["content"][0]["type"] = "output_text"
    elif case == "bad_path":
        payload = final_answer("/root/../bad")
    assert terminal_children(payload, {}) == []


def test_generic_child_spawn_followup_and_final_answer_are_observed():
    calls = {}
    spawn = {**CALL, "name": "collaboration.spawn_agent"}
    terminal_children(spawn, calls)
    result = {"type": "function_call_output", "call_id": "list-call", "output": json.dumps({"agent_id": "/root/consumer"})}
    assert terminal_children(result, calls) == [{"key": "/root/consumer", "status": "running", "discovered": True,
                                                "invocation_id": "list-call"}]
    assert terminal_children(final_answer("/root/consumer"), {}) == [
        {"key": "/root/consumer", "status": "completed", "discovered": True}]
    terminal_children({**CALL, "name": "followup_task", "arguments": '{"target":"/root/consumer","message":"PRIVATE"}'}, calls)
    assert "PRIVATE" not in json.dumps(calls)
    assert terminal_children({**result, "output": "{}"}, calls)[0]["status"] == "running"


def test_spawn_retains_native_identity_beside_canonical_task_path():
    calls, native = {}, str(uuid4())
    terminal_children({**CALL, "name": "spawn_agent", "arguments": '{"task_name":"consumer","message":"PRIVATE"}'}, calls)
    assert "PRIVATE" not in str(calls)
    event, = terminal_children({"type": "function_call_output", "call_id": "list-call",
        "output": json.dumps({"agent_id": native})}, calls)
    assert event["key"] == "/root/consumer"
    assert event["native_id"] == native


def test_explicit_rollout_counters_are_recorded_without_last_turn_double_counting(observer):
    reader, source, native, operations = observer
    payload = {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 20},
                                                  "last_token_usage": {"input_tokens": 50, "output_tokens": 5}}}
    with source.open("a") as stream:
        stream.write(record(payload, "event_msg"))
    assert reader.poll(native, force=True) == 1
    operation, = operations.values()
    raw = operation.payload["raw"]
    assert raw["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert raw["accounting"]["identity"] == native
    assert raw["accounting"]["source"] == "rollout_total_token_usage"
    assert "last_token_usage" not in str(raw)


@pytest.mark.parametrize("version", ["0.153.4", "unknown"])
def test_completion_scope_requires_matching_explicit_native_totals(observer, version):
    reader, source, native, operations = observer
    source.write_text(record({"id": native, "cli_version": version}, "session_meta"))
    raw = {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}
    assert "accounting" not in reader.annotate(raw, native)
    with source.open("a") as stream:
        stream.write(record({"type": "token_count", "info": {"total_token_usage": raw["usage"]}}, "event_msg"))
    event = reader.annotate(raw, native)
    assert event["accounting"]["source"] == "rollout_correlated_completion"
    assert "accounting" not in reader.annotate({**raw, "usage": {"input_tokens": 20, "output_tokens": 3}}, native)
