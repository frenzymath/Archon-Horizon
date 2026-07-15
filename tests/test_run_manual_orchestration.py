"""`horizon run horizon --run <id> --round <n>`: manual single-session driving.

The RunRecord handed to the orchestrator must carry the caller's run id and
round so hand-driven sessions append to one run with consistent numbering,
instead of each allocating a fresh run.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.commands.run import RunCommand
from archon_horizon.core.sessions import RunRecord


class _CapturingOrch:
    """Stands in for the orchestrator: records the RunRecord it is asked to run."""

    def __init__(self) -> None:
        self.captured: RunRecord | None = None

    def run(self, run: RunRecord, *, dry_run: bool = False, resume: bool = False):
        self.captured = run
        return []


def test_single_role_horizon_uses_run_and_round(tmp_path: Path) -> None:
    cmd = RunCommand(tmp_path, targets=("horizon",), run_id="0007", round_index=4)
    orch = _CapturingOrch()

    cmd._run_single_role(orch, "horizon")

    assert orch.captured is not None
    assert orch.captured.id == "0007"
    assert orch.captured.start_round == 4
    assert orch.captured.rounds_requested == 1


def test_single_role_defaults_allocate_fresh_run(tmp_path: Path) -> None:
    # No --run/--round: empty id (orchestrator allocates a fresh run) and round 0.
    cmd = RunCommand(tmp_path, targets=("horizon",))
    orch = _CapturingOrch()

    cmd._run_single_role(orch, "horizon")

    assert orch.captured.id == ""
    assert orch.captured.start_round == 0
