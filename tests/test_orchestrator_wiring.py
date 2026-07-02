"""Orchestrator wiring: agent freeze and audit telemetry."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.vcs.git import WorkspaceGit

BASE = """
workspace:
  name: w
  rounds: 1
  ground_agent: {{harness: inf}}
  horizon_agent: {{harness: hor}}
harnesses:
  inf: {{kind: "null"}}
  hor: {{kind: "null"}}
projects:
  ag-main: {{path: projects/ag-main{blueprint}}}
{extra}
"""


def _setup(tmp_path: Path, *, blueprint: bool = False, extra: str = "") -> Path:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    bp = ", blueprint: {path: projects/ag-main/blueprint}" if blueprint else ""
    (root / "config.yaml").write_text(BASE.format(blueprint=bp, extra=extra), "utf-8")
    return root


def test_frozen_horizon_agent_blocks_tasks(tmp_path: Path) -> None:
    root = _setup(tmp_path, extra="freeze:\n  agents: [horizon]\n")
    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    # A human task exists; the frozen Horizon agent blocks it instead of running it.
    orch.task_store.put(HorizonTask(
        id="R-1", project="ag-main", objective="x", title="x",
        projects=("ag-main",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("ag-main",)),
    ))
    reports = orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert reports[0].tasks_run == ()
    assert orch.task_store.get("R-1").status is TaskStatus.BLOCKED


def test_blueprint_checks_do_not_create_inbox_items(tmp_path: Path) -> None:
    root = _setup(tmp_path, blueprint=True)
    bp = root / "projects" / "ag-main" / "blueprint"
    bp.mkdir(parents=True)
    (bp / "ch1.tex").write_text(r"\begin{lemma}\label{b}\uses{zzz}\lean{B}" "\nB.\n" r"\end{lemma}", "utf-8")

    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")},
                              inbox_providers=[local])
    orch.run(RunRecord(id="", rounds_requested=1))

    assert local.list_items() == []
    event = next(e for e in orch.event_log.read_all() if e.type == "blueprint.checks.findings")
    assert event.data["count"] > 0
    assert event.data["projects"] == {"ag-main": event.data["count"]}


def test_session_meta_includes_workspace_sha(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    git = WorkspaceGit(root)
    git.init()
    git.commit("initial")
    sha = git.current_sha()

    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")})
    orch.run(RunRecord(id="", rounds_requested=1))

    meta = root / ".archon-horizon" / "runs" / "0001" / "sessions" / "0001-ground" / "meta.json"
    assert meta.exists()
    assert sha
    assert sha in meta.read_text("utf-8")
