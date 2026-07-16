"""`horizon run …` reduces every launch shape to one RunRecord (`_build_run`).

`horizon run horizon --run <id> --round <n>` must carry the caller's run id and
round so hand-driven sessions append to one run with consistent numbering,
instead of each allocating a fresh run; a plain focused run takes the
configured round count.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.commands.run import RunCommand


class _UntouchedOrch:
    """A role-only launch must not need the orchestrator's stores at all."""

    def __getattr__(self, name):  # pragma: no cover - only trips on regression
        raise AssertionError(f"orchestrator.{name} should not be touched for a bare role target")


def test_single_role_horizon_uses_run_and_round(tmp_path: Path) -> None:
    cmd = RunCommand(tmp_path, targets=("horizon",), run_id="0007", round_index=4)

    run = cmd._build_run(_UntouchedOrch(), default_rounds=5)

    assert run.id == "0007"
    assert run.start_round == 4
    assert run.rounds_requested == 1  # a bare role target is one session


def test_single_role_defaults_allocate_fresh_run(tmp_path: Path) -> None:
    # No --run/--round: empty id (orchestrator allocates a fresh run) and round 0.
    cmd = RunCommand(tmp_path, targets=("horizon",))

    run = cmd._build_run(_UntouchedOrch(), default_rounds=5)

    assert run.id == ""
    assert run.start_round == 0
    assert run.rounds_requested == 1


def test_supervisor_without_targets_takes_configured_rounds(tmp_path: Path) -> None:
    cmd = RunCommand(tmp_path, targets=(), supervisor=True)

    run = cmd._build_run(_UntouchedOrch(), default_rounds=7)

    assert run.id == ""
    assert run.rounds_requested == 7
    assert run.focus.tasks == ()  # empty focus = all queued work
