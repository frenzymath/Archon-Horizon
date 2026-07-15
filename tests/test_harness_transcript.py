"""Harnesses emit a canonical transcript into the request's artifact_dir."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from archon_horizon.harnesses.base import HarnessRequest
from archon_horizon.harnesses.command import CommandHarness, _classify_failure
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.transcript.model import TranscriptKind
from archon_horizon.transcript.parsers import claude_session_id, parse_claude_line
from archon_horizon.transcript.sink import read_transcript


def test_command_harness_reclaims_pipes_when_a_subagent_holds_them(tmp_path: Path) -> None:
    """Regression: a subprocess can spawn a grandchild that inherits and holds the
    stdout/stderr pipe open after the main process exits (exactly what engine
    subagents do). The harness must still close the parent's pipe FDs each run —
    otherwise it leaks two per run until the system FD table is exhausted
    (ENFILE / "Too many open files in system")."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():
        pytest.skip("FD accounting needs /proc (Linux)")

    # The main process exits immediately, but its grandchild keeps the STDERR pipe
    # open and lingers (longer than the whole test). stdout still reaches EOF (so
    # the harness finishes streaming normally), while the stderr reader stays
    # blocked — the exact production shape, where a lingering MCP server / subagent
    # holds a pipe. Only reaping the engine's process group frees the FDs.
    script = (
        "import sys, subprocess\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "                 stdout=subprocess.DEVNULL)\n"
        "print('done', flush=True)\n"
        "sys.exit(0)\n"
    )
    harness = CommandHarness("hold", [sys.executable, "-c", script])

    def open_fds() -> int:
        return len(list(fd_dir.iterdir()))

    harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "warm"))
    before = open_fds()
    for i in range(3):
        harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / f"run{i}"))
    after = open_fds()
    # Without the fix each run leaks ~two FDs (the blocked stderr reader pins the
    # parent's pipe ends); reaping the group frees them, so the count stays flat.
    assert after - before <= 2


def test_null_harness_writes_transcript(tmp_path: Path) -> None:
    result = NullHarness("hello").run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))
    transcript = tmp_path / "a" / "transcript.jsonl"
    assert result.artifact_refs == (transcript.as_posix(),)
    kinds = [e.kind for e in read_transcript(transcript)]
    assert kinds == [TranscriptKind.SESSION_START, TranscriptKind.TEXT, TranscriptKind.SESSION_END]


def test_command_harness_streams_lines(tmp_path: Path) -> None:
    # A real subprocess that prints two lines, parsed as plain text.
    harness = CommandHarness("echo", [sys.executable, "-c", "print('one'); print('two')"])
    result = harness.run(HarnessRequest(prompt="ignored", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert result.ok
    # result.text is the final message (last text event), not every line joined;
    # the full stream is asserted on the transcript below.
    assert result.text == "two"

    events = read_transcript(tmp_path / "a" / "transcript.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[0] is TranscriptKind.SESSION_START
    assert kinds[-1] is TranscriptKind.SESSION_END
    texts = [e.text for e in events if e.kind is TranscriptKind.TEXT]
    assert texts == ["one", "two"]


def test_command_harness_captures_engine_session_id(tmp_path: Path) -> None:
    # The engine stamps a session id on its stream; the harness records it on the
    # result and on the transcript's session_end (for a later native --resume).
    script = (
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'sess-abc'}))\n"
        "print(json.dumps({'type': 'result', 'result': 'done', 'session_id': 'sess-abc'}))\n"
    )
    harness = CommandHarness(
        "claude", [sys.executable, "-c", script],
        parser=parse_claude_line, session_id_of=claude_session_id,
    )
    result = harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert result.ok
    assert result.metadata["session_id"] == "sess-abc"
    events = read_transcript(tmp_path / "a" / "transcript.jsonl")
    end = [e for e in events if e.kind is TranscriptKind.SESSION_END][-1]
    assert end.data.get("session_id") == "sess-abc"
    # The id is also emitted live (session_meta) so a crashed run stays resumable.
    assert any(e.kind is TranscriptKind.SESSION_META for e in events)


def test_command_harness_captures_model_from_stream(tmp_path: Path) -> None:
    # Claude reports the model it resolved on its init line and final result —
    # even when no --model was configured. The harness must surface it on the
    # result metadata (→ session meta → run view) and on the transcript.
    script = (
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's', 'model': 'claude-opus-4-x'}))\n"
        "print(json.dumps({'type': 'assistant', 'message': {'model': 'claude-opus-4-x', "
        "'content': [{'type': 'text', 'text': 'hi'}]}}))\n"
        "print(json.dumps({'type': 'result', 'result': 'done', 'usage': {'input_tokens': 5, 'output_tokens': 2}, "
        "'modelUsage': {'claude-opus-4-x': {'inputTokens': 5, 'outputTokens': 2}}}))\n"
    )
    harness = CommandHarness("claude", [sys.executable, "-c", script], parser=parse_claude_line)
    result = harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert result.ok
    assert result.metadata["model"] == "claude-opus-4-x"
    events = read_transcript(tmp_path / "a" / "transcript.jsonl")
    assert any(e.data.get("model") == "claude-opus-4-x" for e in events)


def test_classify_failure_flags_claude_session_limit_as_usage_limit() -> None:
    # Claude Code's subscription session cap; retrying in-process can't clear it,
    # so it must be a hard stop (usage_limit), not a retryable rate-limit.
    assert _classify_failure("You've hit your session limit · resets 6:40am (UTC)") == "usage_limit"


def test_classify_failure_flags_server_error_mid_response_as_retryable() -> None:
    # Claude Code's transient stream error ("Server error", a space — not the
    # "server_error"/"internal server error" the old regex looked for) must be a
    # retryable server_error so the session is retried, not hard-failed.
    from archon_horizon.harnesses.command import _RETRYABLE_REASONS

    text = "API Error: Server error mid-response. The response above may be incomplete."
    assert _classify_failure(text) == "server_error"
    assert "server_error" in _RETRYABLE_REASONS


def test_server_error_mid_response_is_retried_not_hard_failed(tmp_path: Path) -> None:
    """A "Server error mid-response" exhausting its retries lands as a retryable
    server_error (i.e. it went through the retry path), not an unclassified hard
    stop — the reported "it stopped the session instead of trying again"."""
    harness = CommandHarness(
        "claude",
        [sys.executable, "-c",
         "import sys; sys.stderr.write('API Error: Server error mid-response. "
         "The response above may be incomplete.'); sys.exit(1)"],
    )
    harness.retry_max = 1
    harness.retry_base_seconds = 0.0  # keep the backoff sleeps out of the test
    result = harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert not result.ok
    assert result.metadata["failure_reason"] == "server_error"
    assert result.metadata["retries_exhausted"] is True


def test_command_harness_flags_instant_outputless_failure_as_aborted_early(tmp_path: Path) -> None:
    # An engine that exits non-zero immediately with no output (a refusal whose
    # wording we don't match) is a hard stop the run loop must halt on, not retry.
    harness = CommandHarness("boom", [sys.executable, "-c", "import sys; sys.exit(3)"])
    result = harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))
    assert not result.ok
    assert result.metadata["failure_reason"] == "aborted_early"


def test_rate_limit_exhausting_retries_is_fatal_on_result_metadata(tmp_path: Path) -> None:
    """A rate/overload limit that survives every retry must halt the whole run.

    The run loop's fatal-failure check reads ``result.metadata`` (never the
    transcript), so ``retries_exhausted`` has to land there — otherwise each
    round respawns a session that hits the same limit and fails again, exactly
    the reported "it carries on failing each session".
    """
    from archon_horizon.orchestration.orchestrator import _is_fatal_failure

    harness = CommandHarness(
        "claude", [sys.executable, "-c", "import sys; sys.stderr.write('Error: 429 rate limit exceeded'); sys.exit(1)"],
    )
    harness.retry_max = 1
    harness.retry_base_seconds = 0.0  # keep the backoff sleeps out of the test
    result = harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert not result.ok
    assert result.metadata["failure_reason"] == "rate_limit"
    assert result.metadata["retries_exhausted"] is True
    assert _is_fatal_failure(result.metadata) is True


def test_command_harness_surfaces_effort_on_metadata_and_transcript(tmp_path: Path) -> None:
    # The reasoning-effort tier the harness ran with must reach the result
    # metadata (→ session meta → run view) and the SESSION_END event so the
    # Logs UI can show it next to the model.
    harness = CommandHarness("claude", [sys.executable, "-c", "print('hi')"])
    harness.horizon_effort = "xhigh"
    result = harness.run(HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a"))

    assert result.metadata["effort"] == "xhigh"
    end = [e for e in read_transcript(tmp_path / "a" / "transcript.jsonl") if e.kind is TranscriptKind.SESSION_END][-1]
    assert end.data.get("effort") == "xhigh"


def test_command_harness_injects_resume_args(tmp_path: Path) -> None:
    from archon_horizon.harnesses.base import HarnessCapability

    # The script echoes its own argv so we can see what the harness passed.
    harness = CommandHarness(
        "claude", [sys.executable, "-c", "import sys; print(' '.join(sys.argv[1:]))"],
        resume_args=lambda sid: ["--resume", sid],
    )
    # Advertising the resume args means advertising the capability.
    assert HarnessCapability.RESUME in harness.capabilities

    resumed = harness.run(HarnessRequest(prompt="go", cwd=tmp_path, resume_session_id="sx"))
    assert "--resume sx" in resumed.text

    fresh = harness.run(HarnessRequest(prompt="go", cwd=tmp_path))
    assert "--resume" not in fresh.text


def test_command_harness_missing_engine_is_graceful(tmp_path: Path) -> None:
    result = CommandHarness("nope", ["definitely-not-a-real-binary-xyz"]).run(
        HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a")
    )
    assert not result.ok
    assert "not found" in result.text
    assert read_transcript(tmp_path / "a" / "transcript.jsonl")[-1].kind is TranscriptKind.SESSION_END
