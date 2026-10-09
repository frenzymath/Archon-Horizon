"""Native harness event parsing used by the Activity collector."""

import json
import pytest

from archon_horizon.pipeline.providers.harness_events import HarnessEvent, HarnessEventKind, HarnessUsage
from archon_horizon.pipeline.providers.harness_parsers import claude_session_id, codex_session_id, parse_claude_line, parse_codex_line, parse_codex_rollout_line, parse_plain_line


def test_plain_parser() -> None:
    assert parse_plain_line("  ") == []
    [event] = parse_plain_line("hello")
    assert event.kind is HarnessEventKind.TEXT and event.text == "hello"


@pytest.mark.parametrize("parser,payload", [
    (parse_claude_line, {"type": "assistant", "message": {"content": None}}),
    (parse_codex_line, {"type": "item.completed", "item": None}),
    (parse_codex_rollout_line, {"type": "response_item", "payload": {"type": "message", "content": 42}}),
    (parse_codex_rollout_line, {"type": "response_item", "payload": {"type": "reasoning", "summary": 42}}),
    (parse_codex_rollout_line, {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": [1]}]}}),
    (parse_codex_rollout_line, {"type": "event_msg", "payload": {"type": "sub_agent_activity", "agent_thread_id": "child", "kind": {}}}),
    (parse_codex_rollout_line, {"type": "event_msg", "payload": {"type": "token_count", "info": [1]}}),
    (parse_codex_rollout_line, {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": [1]}}}),
])
def test_unexpected_native_field_shapes_do_not_abort_parsing(parser, payload):
    assert parser(json.dumps(payload)) == []


def test_malformed_native_counter_values_do_not_abort_later_events():
    events = parse_codex_rollout_line(json.dumps({"type": "event_msg", "payload": {
        "type": "token_count", "info": {"last_token_usage": {
            "input_tokens": "unknown", "output_tokens": float("inf")}}}}))
    assert events[0].usage.tokens_in == 0 and events[0].usage.tokens_out == 0
    assert parse_codex_line(json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": "Still running"}}))[0].text == "Still running"


@pytest.mark.parametrize("role", ["user", "developer", "system"])
def test_rollout_does_not_render_injected_prompts_as_agent_narration(role):
    assert parse_codex_rollout_line(json.dumps({"type": "response_item", "payload": {
        "type": "message", "role": role, "content": [{"type": "text", "text": "Private instructions"}]}})) == []


def test_codex_rollout_collaboration_uses_thread_ids_and_observed_configuration() -> None:
    [spawn] = parse_codex_rollout_line(json.dumps({"type": "event_msg", "payload": {
        "type": "collab_agent_spawn_end", "new_thread_id": "child", "new_agent_nickname": "Reviewer",
        "model": "gpt-test", "reasoning_effort": "high", "prompt": "Private instructions",
    }}))
    assert spawn.kind is HarnessEventKind.SUBAGENT_START
    assert spawn.data["subagent_key"] == "child" and spawn.data["effort"] == "high"
    assert "Private instructions" not in str(spawn.data)
    assert parse_codex_rollout_line(json.dumps({"type": "event_msg", "payload": {
        "type": "collab_agent_spawn_end", "new_thread_id": None,
    }})) == []
    ended = parse_codex_rollout_line(json.dumps({"type": "event_msg", "payload": {
        "type": "collab_waiting_end", "statuses": {"child": {"completed": "Private report"}, "other": "shutdown"},
    }}))
    assert [event.data["status"] for event in ended] == ["completed", "cancelled"]
    assert all(event.kind is HarnessEventKind.SUBAGENT_END for event in ended)
    assert "Private report" not in str(ended)


def test_claude_parser_maps_blocks_and_usage() -> None:
    assistant = json.dumps({
        "type": "assistant",
        "message": {"content": [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "answer"},
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
        ]},
    })
    result = json.dumps({
        "type": "result", "result": "final",
        "usage": {"input_tokens": 10, "output_tokens": 5}, "total_cost_usd": 0.01,
    })
    events = parse_claude_line(assistant) + parse_claude_line(result)
    kinds = [e.kind for e in events]
    assert HarnessEventKind.THINKING in kinds
    assert HarnessEventKind.TOOL_CALL in kinds
    assert [event.text for event in events if event.kind is HarnessEventKind.TEXT] == ["answer", "final"]
    usage_events = [e for e in events if e.kind is HarnessEventKind.USAGE]
    assert usage_events[0].usage == HarnessUsage(tokens_in=10, tokens_out=5, cost_usd=0.01)


def test_codex_parser_and_garbage_is_ignored() -> None:
    msg = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}})
    usage = json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 3,
            "cached_input_tokens": 2,
            "output_tokens": 7,
            "reasoning_output_tokens": 1,
        },
    })
    assert parse_codex_line("not json") == []
    events = parse_codex_line(msg) + parse_codex_line(usage)
    assert events[0].text == "hi"
    assert events[-1].usage == HarnessUsage(
        tokens_in=3,
        tokens_out=7,
        cached_tokens_in=2,
        reasoning_tokens_out=1,
        cost_usd=None,
    )


def test_codex_exec_native_subagent_shape_from_live_luna_probe() -> None:
    spawn = json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "collab_tool_call", "tool": "spawn_agent",
        "sender_thread_id": "parent", "receiver_thread_ids": ["child"],
        "prompt": "Return exactly: native child observed",
        "agents_states": {"child": {"status": "pending_init", "message": None}},
        "status": "completed",
    }})
    events = parse_codex_line(spawn)
    assert [event.kind for event in events] == [HarnessEventKind.TOOL_CALL, HarnessEventKind.SUBAGENT_START]
    assert events[1].data["subagent_key"] == "child"
    assert events[1].data["name"] == "Codex subagent"
    assert events[1].data["description"] == "Return exactly: native child observed"

    wait = json.dumps({"type": "item.completed", "item": {
        "id": "item_2", "type": "collab_tool_call", "tool": "wait",
        "sender_thread_id": "parent", "receiver_thread_ids": ["child"],
        "agents_states": {"child": {"status": "completed", "message": "native child observed"}},
        "status": "completed",
    }})
    ended = parse_codex_line(wait)
    assert [event.kind for event in ended] == [HarnessEventKind.TOOL_CALL, HarnessEventKind.SUBAGENT_END]
    assert ended[1].data["status"] == "completed"
    assert ended[1].data["summary"] == "native child observed"


def test_codex_only_terminal_items_and_command_output() -> None:
    # item.started / item.updated are ignored so commands aren't duplicated;
    # a completed command yields a tool_call + a tool_result with its output.
    started = json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "ls"}})
    updated = json.dumps({"type": "item.updated", "item": {"type": "command_execution", "command": "ls"}})
    completed = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "ls", "aggregated_output": "a\nb", "exit_code": 0},
    })
    assert parse_codex_line(started) == []
    assert parse_codex_line(updated) == []
    events = parse_codex_line(completed)
    assert [e.kind for e in events] == [HarnessEventKind.TOOL_CALL, HarnessEventKind.TOOL_RESULT]
    assert events[0].data["command"] == "ls"
    assert events[1].data["content"] == "a\nb" and events[1].data["exit_code"] == 0


def test_codex_rollout_shell_call_is_clean_command_with_timestamp() -> None:
    # A subagent's shell call arrives as a function_call whose `arguments` is a
    # JSON *string*; it must render as a clean command (not raw JSON), and carry
    # the line's real timestamp (so events keep order instead of clustering).
    line = json.dumps({
        "timestamp": "2026-06-30T09:07:30.324Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "shell",
            "arguments": json.dumps({"cmd": ["bash", "-lc", "pwd && ls"], "workdir": "/x"}),
        },
    })
    [event] = parse_codex_rollout_line(line)
    assert event.kind is HarnessEventKind.TOOL_CALL and event.tool == "Bash"
    assert event.data == {"command": "bash -lc pwd && ls"}
    assert event.at.isoformat() == "2026-06-30T09:07:30.324000+00:00"


def test_codex_rollout_non_shell_call_decodes_input_object() -> None:
    line = json.dumps({
        "type": "response_item",
        "payload": {"type": "function_call", "name": "update_plan",
                    "arguments": json.dumps({"plan": [{"step": "x", "status": "pending"}]})},
    })
    [event] = parse_codex_rollout_line(line)
    assert event.tool == "update_plan"
    assert event.data["input"] == {"plan": [{"step": "x", "status": "pending"}]}  # decoded, not a blob


def test_codex_rollout_session_meta_surfaces_model_and_role() -> None:
    line = json.dumps({
        "type": "session_meta",
        "payload": {"model": "gpt-5.5", "agent_role": "janitor", "agent_nickname": "Ramanujan"},
    })
    [event] = parse_codex_rollout_line(line)
    assert event.kind is HarnessEventKind.SESSION_META
    assert event.data["model"] == "gpt-5.5" and event.data["agent_role"] == "janitor"


def test_codex_rollout_turn_context_surfaces_effort() -> None:
    # Codex records the reasoning-effort tier it actually ran with on turn_context;
    # observed_effort scans it back so the run view can verify it (not just config).
    from archon_horizon.pipeline.providers.harness_parsers import observed_effort

    line = json.dumps({
        "type": "turn_context",
        "payload": {"model": "gpt-5.5", "effort": "xhigh"},
    })
    [event] = parse_codex_rollout_line(line)
    assert event.data["effort"] == "xhigh"
    assert observed_effort([event]) == "xhigh"
    # An engine that reports no effort (e.g. Claude Code) yields None.
    assert observed_effort([HarnessEvent(HarnessEventKind.SESSION_META, data={"model": "x"})]) is None


def test_observed_model_ignores_nested_subagent_events() -> None:
    """Parent session chip must not inherit the first subagent's model (I: luna→gpt)."""
    from archon_horizon.pipeline.providers.harness_parsers import observed_model

    events = [
        HarnessEvent(
            HarnessEventKind.SESSION_META,
            data={
                "model": "luna-something",
                "subagent_type": "janitor",
                "subagent_thread_id": "t-child",
            },
        ),
        HarnessEvent(
            HarnessEventKind.TEXT,
            text="child work",
            data={"model": "luna-something", "subagent_thread_id": "t-child"},
        ),
        HarnessEvent(
            HarnessEventKind.SUBAGENT_START,
            data={"model": "luna-something", "name": "janitor", "subagent_key": "t-child"},
        ),
        HarnessEvent(
            HarnessEventKind.SESSION_META,
            data={"model": "gpt-5.6-sol", "effort": "high"},
        ),
    ]
    assert observed_model(events) == "gpt-5.6-sol"
    # Subagent-only stream: no parent model announced.
    assert observed_model(events[:3]) is None


def test_native_session_id_extractors() -> None:
    # Claude stamps session_id on every event; codex announces it once on
    # thread.started. Both ignore unrelated lines and garbage.
    assert claude_session_id(json.dumps({"type": "system", "session_id": "s1"})) == "s1"
    assert claude_session_id(json.dumps({"type": "assistant"})) is None
    assert claude_session_id("not json") is None
    assert codex_session_id(json.dumps({"type": "thread.started", "thread_id": "t1"})) == "t1"
    assert codex_session_id(json.dumps({"type": "item.completed", "item": {}})) is None
    assert codex_session_id("not json") is None
