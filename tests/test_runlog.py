"""Per-run numbered logs: ordering, nesting, and atomic conflict-free claims."""

from __future__ import annotations

import threading
from pathlib import Path

from archon_horizon.runlog import RunLogTree


def test_runs_are_numbered_and_sessions_ordered(tmp_path: Path) -> None:
    tree = RunLogTree(tmp_path / "runs")
    first = tree.allocate()
    second = tree.allocate()
    assert first.id == "0001" and second.id == "0002"

    s1 = first.new_session("ground")
    s2 = first.new_session("horizon-T-0007")
    assert s1.name == "0001-ground"
    assert s2.name == "0002-horizon-T-0007"
    assert [s.name for s in first.sessions()] == ["0001-ground", "0002-horizon-T-0007"]


def test_subagent_sessions_nest(tmp_path: Path) -> None:
    tree = RunLogTree(tmp_path / "runs")
    session = tree.allocate().new_session("ground")
    child = session.new_subsession("blueprint-lint")
    child.transcript_path.write_text("{}\n", "utf-8")
    assert child.path.parent.name == "subagents"
    assert [s.name for s in session.subsessions()] == ["0001-blueprint-lint"]


def test_concurrent_allocation_never_collides(tmp_path: Path) -> None:
    tree = RunLogTree(tmp_path / "runs")
    ids: list[str] = []
    lock = threading.Lock()

    def claim() -> None:
        run_id = tree.allocate().id
        with lock:
            ids.append(run_id)

    threads = [threading.Thread(target=claim) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ids) == len(set(ids)) == 20  # every run got a distinct number


def test_run_numbers_continue_past_four_digits(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    (runs / "9998").mkdir(parents=True)
    (runs / "9999").mkdir()
    tree = RunLogTree(runs)

    run = tree.allocate()

    assert run.id == "10000"
    assert tree.ids() == ["9998", "9999", "10000"]


def test_session_numbers_continue_past_four_digits(tmp_path: Path) -> None:
    run = RunLogTree(tmp_path / "runs").allocate()
    sessions = run.sessions_dir
    (sessions / "9998-ground").mkdir(parents=True)
    (sessions / "9999-horizon").mkdir()

    session = run.new_session("ground")

    assert session.name == "10000-ground"
    assert [s.name for s in run.sessions()] == [
        "9998-ground",
        "9999-horizon",
        "10000-ground",
    ]
