"""Lifecycle-hook delivery for required inbox attention."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import archon_horizon.commands.agent_hook as agent_hook_module
from archon_horizon.commands.agent_hook import hook_response
from archon_horizon.cli import main
from archon_horizon.commands.ps import clear_process_marker, write_process_marker
from archon_horizon.core.inbox import InboxDraft, InboxKind
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def _workspace(tmp_path: Path, monkeypatch) -> tuple[Path, FilesystemInboxProvider]:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    session = root / ".archon-horizon" / "runs" / "0001" / "sessions" / "s1"
    session.mkdir(parents=True)
    monkeypatch.setenv("ARCHON_HORIZON_ROOT", str(root))
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "s1")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION_DIR", str(session))
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_TASK", "T-1")
    monkeypatch.setenv("ARCHON_HORIZON_PROJECTS", "ag-main")
    provider = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    return root, provider


def _payload(event: str, **extra) -> dict:
    return {
        "session_id": "engine-session-1",
        "hook_event_name": event,
        **extra,
    }


def _additional(response: dict) -> str:
    return response["hookSpecificOutput"]["additionalContext"]


_REPORT = """\
## Progress

- Finished the requested checkpoint and recorded the durable result.

## Why I stopped

The bounded objective is complete.
"""


def test_session_start_injects_message_bodies_and_protections(
    tmp_path: Path, monkeypatch,
) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    inbox.create_item(InboxDraft(
        kind=InboxKind.PROTECTION,
        body="Stable API\n\nDo not change Foo.bar while the consumer lane is live.",
        audience="task:T-1",
        author="human",
    ))
    inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body="Need coordination\n\nPlease own the left-hand proof before noon.",
        audience="task:T-1",
        author="human",
        metadata={"conversation": True},
    ))

    response = hook_response(root, _payload("SessionStart"))

    assert response is not None
    context = _additional(response)
    assert "REQUIRED PROTECTIONS" in context
    assert "Do not change Foo.bar" in context
    assert "NEW DIRECT CONVERSATION" in context
    assert "Please own the left-hand proof" in context
    assert "inbox show I-0002" in context


def test_new_reply_is_injected_next_boundary_then_repeats_every_twenty_calls(
    tmp_path: Path, monkeypatch,
) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    item = inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body="Live thread\n\nCan you check the certificate?",
        audience="task:T-1",
        author="human",
        metadata={"conversation": True},
    ))
    assert hook_response(root, _payload("SessionStart")) is not None

    scans = 0
    original_attention_items = agent_hook_module._attention_items

    def counted_attention_items(path: Path):
        nonlocal scans
        scans += 1
        return original_attention_items(path)

    monkeypatch.setattr(agent_hook_module, "_attention_items", counted_attention_items)

    for _ in range(19):
        assert hook_response(root, _payload("PostToolUse")) is None
    reminder = hook_response(root, _payload("PostToolUse"))
    assert reminder is not None
    assert "HORIZON REMINDER" in _additional(reminder)
    assert scans == 0  # unchanged inbox uses the cached model context

    inbox.add_comment(
        item.id,
        "The other team just found a counterexample.",
        author="ajc-gate",
        metadata={"provenance": {"task": "T-2"}},
    )
    fresh = hook_response(root, _payload("PostToolUse"))
    assert fresh is not None
    assert "counterexample" in _additional(fresh)
    assert "NEW DIRECT CONVERSATION" in _additional(fresh)
    assert scans == 1


def test_unchanged_protection_is_not_periodically_reinjected(
    tmp_path: Path, monkeypatch,
) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    inbox.create_item(InboxDraft(
        kind=InboxKind.PROTECTION,
        body="Stable API\n\nDo not change Foo.bar.",
        audience="task:T-1",
        author="human",
    ))
    assert hook_response(root, _payload("SessionStart")) is not None

    for _ in range(45):
        assert hook_response(root, _payload("PostToolUse")) is None


def test_new_advisory_notification_is_injected_at_next_tool_boundary(
    tmp_path: Path, monkeypatch,
) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    assert hook_response(root, _payload("SessionStart")) is None

    item = inbox.create_item(InboxDraft(
        kind=InboxKind.ISSUE,
        body="New review result\n\nThe generated index is stale.",
        audience="task:T-1",
        author="ajc-gate",
    ))
    fresh = hook_response(root, _payload("PostToolUse"))

    assert fresh is not None
    context = _additional(fresh)
    assert "NEW INBOX NOTIFICATION" in context
    assert "generated index is stale" in context
    assert f"inbox show {item.id}" in context
    assert hook_response(root, _payload("PostToolUse")) is None


def test_parallel_run_start_and_stop_are_injected_once(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)
    assert hook_response(root, _payload("SessionStart")) is None
    other = root / ".archon-horizon" / "runs" / "0002"
    other.mkdir(parents=True)
    write_process_marker(
        other,
        task="T-2",
        task_title="Prove the complementary chart",
    )
    try:
        started = hook_response(root, _payload("PostToolUse"))
        assert started is not None
        context = _additional(started)
        assert "PARALLEL RUNS CHANGED" in context
        assert "started: run 0002 · task T-2" in context
        assert "complementary chart" in context
        assert hook_response(root, _payload("PostToolUse")) is None

        clear_process_marker(other)
        stopped = hook_response(root, _payload("PostToolUse"))
        assert stopped is not None
        assert "stopped: run 0002 · task T-2" in _additional(stopped)
        assert "Active now: 0 run(s)" in _additional(stopped)
        assert hook_response(root, _payload("PostToolUse")) is None
    finally:
        clear_process_marker(other)


def test_mutations_get_compact_commit_checkpoint_and_stop_guard(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)
    assert hook_response(root, _payload(
        "PostToolUse", tool_name="Edit", tool_input={"file_path": "Foo.lean"},
    )) is None
    for _ in range(19):
        assert hook_response(root, _payload(
            "PostToolUse", tool_name="Bash", tool_input={"command": "lake env lean Foo.lean"},
        )) is None
    reminder = hook_response(root, _payload(
        "PostToolUse", tool_name="Bash", tool_input={"command": "lake env lean Foo.lean"},
    ))
    assert reminder is not None
    assert "coherent edits are still uncommitted" in _additional(reminder)

    blocked = hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    ))
    assert blocked is not None and blocked["decision"] == "block"
    assert "COMMIT CHECKPOINT" in blocked["reason"]

    assert hook_response(root, _payload(
        "PostToolUse", tool_name="Bash",
        tool_input={"command": '$HORIZON_GIT commit -m "checkpoint" -- Foo.lean'},
    )) is None
    assert hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    )) is None


def test_read_conversation_stops_reminders_and_unread_stop_continues_once(
    tmp_path: Path, monkeypatch,
) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    item = inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body="Please answer\n\nThis should not be silently abandoned.",
        audience="task:T-1",
        author="human",
        metadata={"conversation": True},
    ))

    blocked = hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    ))
    assert blocked is not None
    assert blocked["decision"] == "block"
    assert "silently abandoned" in blocked["reason"]
    assert hook_response(root, _payload("Stop", stop_hook_active=True)) is None

    inbox.set_read(item.id, "T-1")
    assert hook_response(root, _payload("PostToolUse")) is None
    assert hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    )) is None


def test_headless_stop_requires_report_once_but_interactive_turn_does_not(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)

    blocked = hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message="Done.",
    ))
    assert blocked is not None and blocked["decision"] == "block"
    assert "REPORT CHECKPOINT" in blocked["reason"]
    assert hook_response(root, _payload(
        "Stop", stop_hook_active=True, last_assistant_message="Still done.",
    )) is None

    assert hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    )) is None
    monkeypatch.setenv("ARCHON_HORIZON_INTERACTIVE", "1")
    assert hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message="Short turn reply.",
    )) is None


def test_stop_combines_commit_and_report_cleanup_in_one_retry(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)
    assert hook_response(root, _payload(
        "PostToolUse", tool_name="Edit", tool_input={"file_path": "Foo.lean"},
    )) is None

    blocked = hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message="Done.",
    ))

    assert blocked is not None
    assert "COMMIT CHECKPOINT" in blocked["reason"]
    assert "REPORT CHECKPOINT" in blocked["reason"]
    assert hook_response(root, _payload(
        "Stop", stop_hook_active=True, last_assistant_message="Done.",
    )) is None


def test_stop_uses_clean_ledger_status_after_observed_commit(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)
    assert hook_response(root, _payload(
        "PostToolUse", tool_name="Edit", tool_input={"file_path": "Foo.lean"},
    )) is None
    assert hook_response(root, _payload(
        "PostToolUse", tool_name="Bash",
        tool_input={"command": '$HORIZON_GIT commit -m "partial" -- Foo.lean'},
    )) is None
    monkeypatch.setattr(
        agent_hook_module,
        "_ledger_dirty_paths",
        lambda _root: ("projects/ag-main/Bar.lean",),
    )

    blocked = hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    ))

    assert blocked is not None
    assert "COMMIT CHECKPOINT" in blocked["reason"]
    assert "projects/ag-main/Bar.lean" in blocked["reason"]

    monkeypatch.setattr(agent_hook_module, "_ledger_dirty_paths", lambda _root: ())
    assert hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    )) is None


def test_unread_conversation_pauses_commit_but_not_other_commands(
    tmp_path: Path, monkeypatch,
) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    item = inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body="Commit checkpoint\n\nCheck with this team before landing.",
        audience="task:T-1",
        author="human",
        metadata={"conversation": True},
    ))
    assert hook_response(root, _payload(
        "PreToolUse", tool_name="Bash", tool_input={"command": "lake build"},
    )) is not None  # new context is injected, but the command is not denied

    blocked = hook_response(root, _payload(
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": 'cd project && git commit -m "finish"'},
    ))
    assert blocked is not None
    output = blocked["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny"
    assert "Commit paused" in output["permissionDecisionReason"]

    inbox.set_read(item.id, "T-1")
    assert hook_response(root, _payload(
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": 'git commit -m "finish"'},
    )) is None


def test_hidden_cli_emits_only_hook_json(tmp_path: Path, monkeypatch, capsys) -> None:
    root, inbox = _workspace(tmp_path, monkeypatch)
    inbox.create_item(InboxDraft(
        kind=InboxKind.CONVERSATION,
        body="CLI delivery\n\nThis must be valid hook JSON.",
        audience="task:T-1",
        author="human",
        metadata={"conversation": True},
    ))
    monkeypatch.setenv("ARCHON_HORIZON_NO_SYNC", "1")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps(_payload("SessionStart")))
    )

    assert main(["--root", str(root), "agent-hook", "--json"]) == 0

    response = json.loads(capsys.readouterr().out)
    assert "CLI delivery" in _additional(response)


def test_horizon_state_mutations_and_failed_commits_remain_dirty(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)
    mutation = _payload(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": '"$HORIZON_BIN" task comment T-1 --body "checkpoint"'},
    )
    assert hook_response(root, mutation) is None
    failed_commit = _payload(
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": '$HORIZON_GIT commit -m "checkpoint" -- .archon-horizon/tasks'},
        tool_response={"exit_code": 1},
    )
    assert hook_response(root, failed_commit) is None

    blocked = hook_response(root, _payload(
        "Stop", stop_hook_active=False, last_assistant_message=_REPORT,
    ))
    assert blocked is not None
    assert "COMMIT CHECKPOINT" in blocked["reason"]


def test_elapsed_time_can_trigger_commit_and_progress_checkpoints(
    tmp_path: Path, monkeypatch,
) -> None:
    root, _ = _workspace(tmp_path, monkeypatch)
    now = [100.0]
    monkeypatch.setattr(agent_hook_module.time, "time", lambda: now[0])
    monkeypatch.setenv("ARCHON_HORIZON_COMMIT_REMINDER_SECONDS", "10")
    assert hook_response(root, _payload(
        "PostToolUse", tool_name="Edit", tool_input={"file_path": "Foo.lean"},
    )) is None
    now[0] += 11
    reminder = hook_response(root, _payload(
        "PostToolUse", tool_name="Bash", tool_input={"command": "lake env lean Foo.lean"},
    ))
    assert reminder is not None
    assert "COMMIT CHECKPOINT" in _additional(reminder)

    root2, _ = _workspace(tmp_path / "other", monkeypatch)
    now[0] = 200.0
    monkeypatch.setattr(agent_hook_module, "_PROGRESS_REMINDER_EVERY", 2)
    monkeypatch.setenv("ARCHON_HORIZON_PROGRESS_REMINDER_SECONDS", "10")
    assert hook_response(root2, _payload(
        "PostToolUse", tool_name="Bash", tool_input={"command": "rg theorem Foo.lean"},
    )) is None
    now[0] += 11
    progress = hook_response(root2, _payload(
        "PostToolUse", tool_name="Bash", tool_input={"command": "rg lemma Foo.lean"},
    ))
    assert progress is not None
    assert "PROGRESS CHECKPOINT" in _additional(progress)
