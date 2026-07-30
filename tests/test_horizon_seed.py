"""The interactive seed only tells the agent to load the `horizon` skill and
wait for the user — no composed role brief / pushed policy.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.commands.interactive import discuss_prompt, horizon_seed_prompt


def test_seed_loads_the_skill_and_waits() -> None:
    seed = horizon_seed_prompt(Path("/ws"))
    assert "`horizon`" in seed and "skill" in seed          # points at the skill
    assert "wait" in seed.lower()                            # then yields to the user
    assert "/ws" in seed                                     # names the workspace root
    assert "README.md" not in seed  # orientation is pulled via the skill, not pushed


def test_seed_threads_focus_when_present() -> None:
    seed = horizon_seed_prompt(Path("/ws"), focus=("T-7", "projA/File.lean"))
    assert "T-7" in seed and "projA/File.lean" in seed
    assert "skill" in seed  # still loads the skill first


def test_seed_resuming_frames_continuation() -> None:
    seed = horizon_seed_prompt(Path("/ws"), focus=("T-7",), resuming=True)
    assert "RESUMING" in seed
    assert "T-7" in seed
    assert "skill" in seed
    assert "$ARCHON_HORIZON_SESSION_DIR/.." in seed
    assert "report.md" in seed and "transcript" in seed


def test_discuss_is_the_consent_gated_coordination_console() -> None:
    seed = discuss_prompt(Path("/ws"))
    assert "coordination console" in seed
    assert "horizon ps" in seed
    assert "horizon permissions --json" in seed
    assert "explicitly approves" in seed
    assert "tmux" in seed and "max_parallel_sessions" in seed
