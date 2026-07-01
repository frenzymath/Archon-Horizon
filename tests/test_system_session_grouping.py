"""Deterministic 'system' work between agents shows up as ONE session.

When no Horizon runs (e.g. the run stops early), the opening-Ground aftermath and
the final run-integration must not split into two tiny consecutive system
sessions — they belong to one open system session.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import RunRecord
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_consecutive_system_work_is_one_session(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(
        root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")}, inbox_providers=[local]
    )
    # No active roadmap items → opening Ground runs, the round finds no task and
    # the run stops; the only deterministic work is the opening aftermath + the
    # final integration.
    orch.run(RunRecord(id="", rounds_requested=1))

    sessions_dir = root / ".archon-horizon" / "runs" / "0001" / "sessions"
    names = sorted(p.name for p in sessions_dir.iterdir())
    system = [n for n in names if n.endswith("-system")]
    assert len(system) == 1, f"expected one grouped system session, got {system}"
    # And it is a real, finalised session (its transcript carries a session_end).
    from archon_horizon.transcript.sink import read_transcript
    from archon_horizon.transcript.model import TranscriptKind

    events = read_transcript(sessions_dir / system[0] / "transcript.jsonl")
    assert any(e.kind is TranscriptKind.SESSION_END for e in events)
