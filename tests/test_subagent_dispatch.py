"""Archon-style descriptor subagents dispatch through harnessed transcripts."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.subagents.registry import parse_descriptor_file
from archon_horizon.transcript.model import TranscriptKind
from archon_horizon.transcript.sink import read_transcript


CONFIG = """
workspace:
  name: w
  rounds: 1
  informal_agent:
    harness: inf
    subagents: [echo]
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects: {}
"""


def _workspace(root: Path) -> None:
    (root / "config.yaml").write_text(CONFIG, "utf-8")
    subagents = root / ".archon-horizon" / "subagents"
    subagents.mkdir(parents=True)
    (subagents / "echo.md").write_text(
        "---\n"
        "name: echo\n"
        "description: Echo a directive into a report.\n"
        "read_only: true\n"
        "---\n"
        "Return a concise report.\n",
        "utf-8",
    )


def test_descriptor_file_parses_frontmatter(tmp_path: Path) -> None:
    _workspace(tmp_path)
    descriptor = parse_descriptor_file(tmp_path / ".archon-horizon" / "subagents" / "echo.md")
    assert descriptor.name == "echo"
    assert descriptor.read_only
    assert "concise report" in descriptor.prompt_body


def test_subagent_cli_creates_nested_transcript(tmp_path: Path) -> None:
    _workspace(tmp_path)
    parent = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions" / "0001-informal"
    parent.mkdir(parents=True)
    directive = parent / "echo-directive.md"
    directive.write_text("Report on the roadmap.", "utf-8")

    code = main([
        "--root",
        str(tmp_path),
        "subagent",
        "echo",
        "--slug",
        "roadmap",
        "--directive-file",
        str(directive),
        "--parent-log-dir",
        str(parent),
        "--write-domain",
        "reports/**",
    ])

    assert code == 0
    transcript = parent / "subagents" / "0001-echo" / "transcript.jsonl"
    events = read_transcript(transcript)
    assert [event.kind for event in events] == [TranscriptKind.SESSION_START, TranscriptKind.SESSION_END]


def test_orchestrator_keeps_harnessed_subagent_transcript_single(tmp_path: Path) -> None:
    _workspace(tmp_path)
    orch = build_orchestrator(
        tmp_path,
        harnesses={
            "inf": NullHarness("subagent report"),
            "hor": NullHarness(""),
        },
    )

    orch.run(RunRecord(id="", rounds_requested=1))

    transcript = (
        tmp_path
        / ".archon-horizon"
        / "runs"
        / "0001"
        / "sessions"
        / "0001-round0-informal"
        / "subagents"
        / "0001-echo"
        / "transcript.jsonl"
    )
    events = read_transcript(transcript)
    assert [event.kind for event in events] == [
        TranscriptKind.SESSION_START,
        TranscriptKind.TEXT,
        TranscriptKind.SESSION_END,
    ]
