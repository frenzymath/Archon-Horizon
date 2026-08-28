"""`horizon ps`: the per-run process registry — list, reap zombies."""

from __future__ import annotations

import json
import os
from pathlib import Path

from archon_horizon.cli import main

_CONFIG = "workspace:\n  name: w\n  horizon_agent: {harness: hor}\nharnesses:\n  hor: {kind: \"null\"}\nprojects: {}\n"


def _marker(ws: Path, run_id: str, pid: int) -> Path:
    run_dir = ws / ".archon-horizon" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "process.json"
    path.write_text(json.dumps({"pid": pid, "host": __import__("socket").gethostname(),
                                "started_at": "2026-07-16T00:00:00+00:00"}), "utf-8")
    return path


def test_ps_lists_live_and_auto_reaps_zombies(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    live = _marker(ws, "0001", os.getpid())        # alive: this test process
    dead = _marker(ws, "0002", 2 ** 22 + 12345)    # almost certainly dead
    # Leave a still-running session meta under the dead run so reaping must
    # finalize it to interrupted (session-states hygiene).
    session = ws / ".archon-horizon" / "runs" / "0002" / "sessions" / "0001-horizon-T"
    session.mkdir(parents=True)
    (session / "meta.json").write_text(
        json.dumps({"status": "running", "role": "horizon"}), "utf-8",
    )

    assert main(["--root", str(ws), "ps", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = {r["run"]: r for r in payload["processes"]}
    assert rows["0001"]["alive"] is True
    assert "0002" not in rows  # auto-reaped
    assert "0002" in payload.get("reaped", [])
    assert live.exists()
    assert not dead.exists()
    meta = json.loads((session / "meta.json").read_text("utf-8"))
    assert meta["status"] == "interrupted"
    assert meta.get("ended_at")


def test_ps_clean_reaps_dead_markers_only(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    live = _marker(ws, "0001", os.getpid())
    dead = _marker(ws, "0002", 2 ** 22 + 12345)

    assert main(["--root", str(ws), "ps", "--clean", "--json"]) == 0
    assert live.exists()
    assert not dead.exists()


def test_run_registers_and_clears_process_marker(tmp_path: Path) -> None:
    from archon_horizon.config.loader import build_orchestrator
    from archon_horizon.core.sessions import Focus, RunRecord
    from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
    from archon_horizon.harnesses.base import HarnessResult
    from archon_horizon.harnesses.null import NullHarness

    ws = tmp_path / "ws"
    (ws / "projects" / "p").mkdir(parents=True)
    (ws / "config.yaml").write_text(
        "workspace:\n  name: w\n  horizon_agent: {harness: hor}\n"
        "harnesses:\n  hor: {kind: \"null\"}\nprojects:\n  p: {path: projects/p}\n",
        "utf-8",
    )
    seen: dict[str, bool] = {}

    def probe(req) -> HarnessResult:
        run_dir = ws / ".archon-horizon" / "runs" / "0001"
        seen["marker_during_run"] = (run_dir / "process.json").exists()
        return HarnessResult(ok=True, text="ok")

    orch = build_orchestrator(ws, harnesses={"hor": NullHarness(probe)})
    orch.task_store.put(HorizonTask(
        id="T-1", project="p", objective="x", title="x",
        projects=("p",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("p",)),
    ))
    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=1))

    assert seen["marker_during_run"] is True
    assert not (ws / ".archon-horizon" / "runs" / "0001" / "process.json").exists()  # cleared on exit
