"""Config layer: parse config.yaml, build harnesses, run a round end-to-end."""

from __future__ import annotations

from pathlib import Path

import pytest

from archon_horizon.config.harnesses import HarnessRegistry, UnknownHarnessKind
from archon_horizon.config.loader import build_orchestrator, build_workspace, load_config
from archon_horizon.config.schema import HarnessConfig
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import TaskStatus
from archon_horizon.harnesses.base import HarnessResult
from archon_horizon.harnesses.command import CommandHarness
from archon_horizon.harnesses.null import NullHarness

CONFIG = """
workspace:
  name: ag-horizon
  state_dir: .archon-horizon
  rounds: 1
  informal_agent:
    harness: informal-default
  horizon_agent:
    harness: horizon-default
  scheduler:
    max_parallel_sessions: 2
harnesses:
  informal-default:
    kind: "null"
  horizon-default:
    kind: codex
    model: fable5
    options:
      effort: high
projects:
  ag-main:
    path: projects/ag-main
    type: lean
    build:
      command: lake build
    freeze:
      files:
        - Frozen.lean
"""


def _write_config(root: Path, body: str = CONFIG) -> None:
    (root / "config.yaml").write_text(body, "utf-8")
    (root / "projects" / "ag-main").mkdir(parents=True)


def test_parses_workspace_and_projects(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.name == "ag-horizon"
    assert cfg.horizon_harness == "horizon-default"
    assert cfg.harnesses["horizon-default"].model == "fable5"

    workspace = build_workspace(cfg, tmp_path)
    assert workspace.project_path("ag-main") == tmp_path / "projects" / "ag-main"


def test_registry_builds_codex_argv(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cfg = load_config(tmp_path)
    harness = HarnessRegistry().build(cfg.harnesses["horizon-default"])
    assert isinstance(harness, CommandHarness)
    argv = harness._argv("PROMPT")  # noqa: SLF001 — asserting the wiring
    assert argv[:2] == ["codex", "exec"]
    assert "fable5" in argv and "model_reasoning_effort=high" in argv
    assert argv[-1] == "PROMPT"


def test_unknown_kind_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(UnknownHarnessKind):
        HarnessRegistry().build(HarnessConfig(name="x", kind="does-not-exist"))


def test_build_orchestrator_runs_with_harness_overrides(tmp_path: Path) -> None:
    _write_config(tmp_path)
    overrides = {
        "informal-default": NullHarness(
            'plan\n```json\n{"tasks": [{"project": "ag-main", "objective": "x", '
            '"write_set": {"files": ["Ok.lean"]}}]}\n```'
        ),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=True, text="done")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    reports = orch.run(RunRecord(id="S-0001", rounds_requested=1))

    assert reports[0].tasks_run == ("T-0001",)
    assert orch.task_store.get("T-0001").status is TaskStatus.DONE
    # Freeze from config is live: a task on Frozen.lean would be blocked.
    assert (tmp_path / ".archon-horizon" / "roadmap.yaml").parent.exists()
