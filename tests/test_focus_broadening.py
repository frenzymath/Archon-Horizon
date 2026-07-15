"""Focus resolution and multi-round re-run of an explicitly focused task.

Tasks are human-created (or inferred on demand from a roadmap id); the roadmap no
longer auto-feeds the work queue. A focused run re-works exactly the task(s) you
named, round after round, and says why if one is not runnable.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.harnesses.base import HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider


def _mark_running_done(store) -> None:
    """Simulate the agent running ``horizon task set <id> --status done`` during
    its session: the orchestrator sets the dispatched task RUNNING before the
    engine starts, so recording the running task as done (with history) is exactly
    what a real agent does — the only way a task becomes terminal now."""
    for task in store.list():
        if task.status is TaskStatus.RUNNING:
            store.put(dataclasses.replace(task, status=TaskStatus.DONE))
            store.append_history(task.id, {
                "at": "2026-07-02T00:00:00+00:00",
                "actor": "horizon",
                "field": "status",
                "from": "running",
                "to": "done",
                "note": "agent recorded status via `horizon task set`",
            })

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


_CONFIG_FROZEN = _CONFIG + "freeze:\n  projects: [ag-main]\n"


def _orchestrator(root: Path, record, *, config: str = _CONFIG):
    (root / "projects" / "ag-main").mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(config, "utf-8")
    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    return build_orchestrator(
        root, harnesses={"inf": NullHarness(""), "hor": NullHarness(record)}, inbox_providers=[local]
    )


def test_focused_task_stops_when_done(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    ran: list[str] = []
    # The agent records `done` itself, so the task actually reaches DONE (a clean
    # exit alone would leave it QUEUED and keep running the remaining rounds).
    holder: dict = {}

    def record(req):
        ran.append(req.prompt)
        _mark_running_done(holder["store"])
        return HarnessResult(ok=True, text="proved it")

    orch = _orchestrator(root, record)
    holder["store"] = orch.task_store
    orch.task_store.put(HorizonTask(
        id="T-1", project="ag-main", objective="prove it", title="prove it",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))

    reports = orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=3))

    # The focused task completes on round 1, so the run stops early instead of
    # reworking a finished milestone across the remaining requested rounds.
    assert tuple(t for r in reports for t in r.tasks_run) == ("T-1",)
    assert len(ran) == 1
    stops = [e for e in orch.event_log.read_all() if e.type == "run.stopped"]
    assert stops and stops[-1].data.get("reason") == "focus-complete"


def test_frozen_focused_task_reports_unrunnable_and_stops(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    orch = _orchestrator(root, lambda req: HarnessResult(ok=True, text="done"), config=_CONFIG_FROZEN)
    orch.task_store.put(HorizonTask(
        id="T-1", project="ag-main", objective="prove it", title="prove it",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))

    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=1))

    types = [e.type for e in orch.event_log.read_all()]
    assert "run.focus_unrunnable" in types
    assert "run.stopped" in types


def test_empty_focus_runs_all_queued_once(tmp_path: Path) -> None:
    # `horizon run *` (empty focus) runs every queued task once; each records
    # `done` so it rests, and nothing is re-queued — so later rounds find nothing
    # runnable.
    root = tmp_path / "ws"
    ran: list[str] = []
    holder: dict = {}

    def record(req):
        ran.append(req.prompt)
        _mark_running_done(holder["store"])
        return HarnessResult(ok=True, text="done")

    orch = _orchestrator(root, record)
    holder["store"] = orch.task_store
    for tid in ("T-1", "T-2"):
        orch.task_store.put(HorizonTask(
            id=tid, project="ag-main", objective="x", title=tid,
            projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
        ))

    reports = orch.run(RunRecord(id="", focus=Focus(), rounds_requested=2))

    ran_ids = [t for r in reports for t in r.tasks_run]
    assert set(ran_ids) == {"T-1", "T-2"}  # both ran
    assert ran_ids.count("T-1") == 1 and ran_ids.count("T-2") == 1  # once each, not re-queued
