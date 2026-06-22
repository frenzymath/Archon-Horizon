"""Harnesses emit a canonical transcript into the request's artifact_dir."""

from __future__ import annotations

import sys
from pathlib import Path

from archon_horizon.harnesses.base import HarnessRequest
from archon_horizon.harnesses.command import CommandHarness
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.transcript.model import TranscriptKind
from archon_horizon.transcript.sink import read_transcript


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
    assert "one" in result.text and "two" in result.text

    events = read_transcript(tmp_path / "a" / "transcript.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[0] is TranscriptKind.SESSION_START
    assert kinds[-1] is TranscriptKind.SESSION_END
    texts = [e.text for e in events if e.kind is TranscriptKind.TEXT]
    assert texts == ["one", "two"]


def test_command_harness_missing_engine_is_graceful(tmp_path: Path) -> None:
    result = CommandHarness("nope", ["definitely-not-a-real-binary-xyz"]).run(
        HarnessRequest(prompt="x", cwd=tmp_path, artifact_dir=tmp_path / "a")
    )
    assert not result.ok
    assert "not found" in result.text
    assert read_transcript(tmp_path / "a" / "transcript.jsonl")[-1].kind is TranscriptKind.SESSION_END
