"""Orchestrator wiring: proposals, agent freeze, and subagent issue creation."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.labels import ARCHON_PENDING
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import Proposal, TaskStatus
from archon_horizon.harnesses.base import HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

BASE = """
workspace:
  name: w
  rounds: 1
  informal_agent: {{harness: inf}}
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


def _ok_horizon():
    return {"inf": NullHarness(""), "hor": NullHarness(lambda r: HarnessResult(ok=True, text="done"))}


def test_proposal_execution_creates_task_and_marks_applied(tmp_path: Path) -> None:
    root = _setup(tmp_path)
    orch = build_orchestrator(root, harnesses=_ok_horizon())
    proposal = orch.proposal_store.put(
        Proposal(id="", title="new work", body="...",
                 metadata={"task": {"project": "ag-main", "objective": "do x", "files": ["F.lean"]}})
    )
    orch.run(RunRecord(id="", focus=Focus(proposal=proposal.id)))

    tasks = orch.task_store.list()
    assert any(t.objective == "do x" for t in tasks)
    assert orch.proposal_store.get(proposal.id).status.value == "applied"


def test_frozen_horizon_agent_blocks_tasks(tmp_path: Path) -> None:
    root = _setup(tmp_path, extra="freeze:\n  agents: [horizon]\n")
    informal = NullHarness('plan\n```json\n{"tasks": [{"project": "ag-main", "objective": "x"}]}\n```')
    orch = build_orchestrator(root, harnesses={"inf": informal, "hor": NullHarness("")})
    reports = orch.run(RunRecord(id="", rounds_requested=1))

    assert reports[0].tasks_run == ()
    assert orch.task_store.get("T-0001").status is TaskStatus.BLOCKED


def test_subagents_create_pending_issues_during_a_run(tmp_path: Path) -> None:
    root = _setup(tmp_path, blueprint=True)
    bp = root / "projects" / "ag-main" / "blueprint"
    bp.mkdir(parents=True)
    (bp / "ch1.tex").write_text(r"\begin{lemma}\label{b}\uses{zzz}\lean{B}" "\nB.\n" r"\end{lemma}", "utf-8")

    local = FilesystemInboxProvider(root / ".archon-horizon" / "inboxes" / "local.yaml")
    orch = build_orchestrator(root, harnesses={"inf": NullHarness(""), "hor": NullHarness("")},
                              inbox_providers=[local])
    orch.run(RunRecord(id="", rounds_requested=1))

    issues = local.list_items()
    assert issues, "dag-consistency should raise the dangling-use issue"
    assert all(ARCHON_PENDING in i.labels for i in issues)  # await human triage
    assert any("zzz" in i.body for i in issues)
