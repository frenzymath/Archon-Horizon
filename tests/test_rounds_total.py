"""`ARCHON_HORIZON_ROUNDS` must bound `ARCHON_HORIZON_ROUND` for THIS invocation.

The agent's only signal that it is the run's last round — and so should leave the
workspace hand-off-clean — is `ROUND + 1 == ROUNDS` (see the `horizon` skill). A
resume drives a fresh batch of rounds on top of what already ran, pushing the
round index past the run's originally-requested count, so `rounds_total` has to
track the rounds this invocation actually drives rather than `rounds_requested`.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.harnesses.null import NullHarness

BASE = """
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


def _orch(tmp_path: Path):
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "config.yaml").write_text(BASE, "utf-8")
    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    # The task records no terminal status, so it returns to QUEUED and is
    # runnable again on the next round — which is what lets a resume pick it up.
    orch.task_store.put(HorizonTask(
        id="T-1", project="ag-main", objective="x", title="x",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))
    return orch


def _spy(orch) -> list[tuple[int | None, int | None]]:
    """Record the (round_index, rounds_total) each Horizon session is handed."""
    seen: list[tuple[int | None, int | None]] = []
    inner = orch.horizon.run_task

    def run_task(context):
        seen.append((context.round_index, context.rounds_total))
        return inner(context)

    orch.horizon.run_task = run_task
    return seen


def test_fresh_run_rounds_total_bounds_round_index(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    seen = _spy(orch)

    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=2))

    assert seen == [(0, 2), (1, 2)]
    # The final round is recognizable as final.
    assert seen[-1][0] + 1 == seen[-1][1]


def test_resumed_run_last_round_is_recognizable(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=1))

    # Round 0 completed above; resume drives a fresh batch of 2 rounds on top,
    # so rounds 1 and 2 run and `rounds_total` must be 3 — not `rounds_requested`.
    seen = _spy(orch)
    orch.run(
        RunRecord(id="0001", focus=Focus(tasks=("T-1",)), rounds_requested=1),
        resume=True,
        rounds_override=2,
    )

    assert seen == [(1, 3), (2, 3)]
    assert seen[-1][0] + 1 == seen[-1][1]


def test_run_appended_at_a_start_round_bounds_correctly(tmp_path: Path) -> None:
    # `horizon run horizon --run <id> --round <n>` appends a session at an offset;
    # round numbering is shifted by `start_round`, so the bound shifts with it.
    orch = _orch(tmp_path)
    seen = _spy(orch)

    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=1, start_round=4))

    assert seen == [(4, 5)]
    assert seen[-1][0] + 1 == seen[-1][1]
