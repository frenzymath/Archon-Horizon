"""Phase 1 (v0.1.1): the lightweight interactive seed only tells the agent to load
the `horizon` skill and wait for the user — no composed role brief / pushed policy.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.commands.interactive import horizon_seed_prompt, interactive_role_prompt


def test_bare_seed_loads_the_skill_and_waits() -> None:
    seed = horizon_seed_prompt(Path("/ws"))
    assert "`horizon`" in seed and "skill" in seed          # points at the skill
    assert "wait" in seed.lower()                            # then yields to the user
    assert "/ws" in seed                                     # names the workspace root


def test_bare_seed_is_lighter_than_the_role_brief() -> None:
    bare = horizon_seed_prompt(Path("/ws"))
    heavy = interactive_role_prompt(Path("/ws"), "horizon")
    # The bare seed pushes far less prose and none of the role-brief policy.
    assert len(bare) < len(heavy)
    assert "Long Horizon agent" not in bare
    assert "README.md" not in bare  # orientation is pulled via the skill, not pushed


def test_bare_seed_threads_focus_when_present() -> None:
    seed = horizon_seed_prompt(Path("/ws"), focus=("T-7", "projA/File.lean"))
    assert "T-7" in seed and "projA/File.lean" in seed
    assert "skill" in seed  # still loads the skill first
