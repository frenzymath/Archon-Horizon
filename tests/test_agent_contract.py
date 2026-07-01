"""Agent prompt/output contract: prompts expose schema, parser consumes it."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.agents.base import HorizonContext, GroundContext
from archon_horizon.agents.parsing import parse_ground_update
from archon_horizon.agents.prompts import compose_horizon_prompt, compose_ground_prompt
from archon_horizon.core.inbox import InboxItem, InboxKind
from archon_horizon.core.labels import AGENT_READY
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.core.workspace import Project, Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        name="ws",
        root=tmp_path,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"), build_command="lake build")},
    )


def test_ground_prompt_contains_archon_horizon_contract(tmp_path: Path) -> None:
    run = RunRecord(id="S-1", rounds_requested=1)
    ctx = GroundContext(
        workspace=_workspace(tmp_path),
        run=run,
        focus=run.focus,
        roadmap=Roadmap(),
        accepted_inbox=(InboxItem(id="I-1", provider="local", kind=InboxKind.HINT, body="use affine", labels=(AGENT_READY,)),),
        blueprint_summary="ag-main: 1 nodes, 0 proved, 0 edges, 0 dangling",
        memory="avoid old lemma",
    )

    prompt = compose_ground_prompt(ctx)

    assert "Ground agent" in prompt
    # No machine-readable report contract anymore, but CLI reads should be parseable.
    assert "--json" in prompt
    assert "--author ground" in prompt
    assert "supervisor" in prompt and "janitor" in prompt
    assert "# Skills" in prompt and "horizon-inbox" in prompt  # capabilities are skills now
    assert "ag-main: 1 nodes" in prompt
    assert "use affine" in prompt


def test_ground_prompt_compacts_large_inbox(tmp_path: Path) -> None:
    items = tuple(
        InboxItem(
            id=f"I-{i:04d}",
            provider="local",
            kind=InboxKind.HINT,
            body=("detail " * 200) + str(i),
            labels=(AGENT_READY,),
        )
        for i in range(12)
    )
    ctx = GroundContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        focus=RunRecord(id="S-1", rounds_requested=1).focus,
        roadmap=Roadmap(),
        accepted_inbox=items,
        blueprint_summary="",
        memory="",
    )

    prompt = compose_ground_prompt(ctx)

    assert "I-0000" in prompt
    assert "I-0009" in prompt
    assert "I-0010" not in prompt
    assert "2 more accepted inbox item(s) omitted" in prompt
    assert "detail " * 120 not in prompt


def test_horizon_prompt_is_task_scoped(tmp_path: Path) -> None:
    task = HorizonTask(
        id="T-1",
        project="ag-main",
        objective="Prove Foo.bar",
        write_set=WriteSet(files=("Foo.lean",)),
        roadmap_refs=("R-1",),
    )
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=task,
        roadmap=Roadmap(),
    )

    prompt = compose_horizon_prompt(ctx)

    assert "Horizon agent" in prompt
    assert "Recommended focus" in prompt
    assert "Prove Foo.bar" in prompt
    assert "files=Foo.lean" in prompt  # carried as "Suggested scope"
    assert "roadmap=R-1" in prompt
    assert "--json" in prompt
    assert "--author horizon" in prompt


class _RecordingHarness:
    """Minimal harness double that records the request it was handed."""

    def __init__(self, *, resume: bool) -> None:
        from archon_horizon.harnesses.base import HarnessCapability

        self.capabilities = frozenset({HarnessCapability.RESUME}) if resume else frozenset()
        self.request = None

    def run(self, request):
        from archon_horizon.harnesses.base import HarnessResult

        self.request = request
        return HarnessResult(ok=True, text="ok")


def test_horizon_resumes_native_session_when_engine_supports_it(tmp_path: Path) -> None:
    from archon_horizon.agents.harness_agents import HORIZON_CONTINUE, HarnessHorizonAgent

    task = HorizonTask(id="T-1", project="ag-main", objective="Prove Foo.bar")
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=task,
        roadmap=Roadmap(),
        resume_session_id="sid-1",
    )

    harness = _RecordingHarness(resume=True)
    HarnessHorizonAgent(harness).run_task(ctx)

    # Native resume: short continuation prompt + the engine session id passed through.
    assert harness.request.resume_session_id == "sid-1"
    assert harness.request.prompt == HORIZON_CONTINUE


def test_horizon_falls_back_to_full_prompt_without_resume_support(tmp_path: Path) -> None:
    from archon_horizon.agents.harness_agents import HORIZON_CONTINUE, HarnessHorizonAgent

    task = HorizonTask(id="T-1", project="ag-main", objective="Prove Foo.bar")
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=task,
        roadmap=Roadmap(),
        resume_session_id="sid-1",
    )

    harness = _RecordingHarness(resume=False)
    HarnessHorizonAgent(harness).run_task(ctx)

    # No native resume: the id is dropped and the full prompt is re-sent.
    assert harness.request.resume_session_id is None
    assert harness.request.prompt != HORIZON_CONTINUE
    assert "Prove Foo.bar" in harness.request.prompt


def test_parse_ground_update_is_report_only() -> None:
    # The whole response IS the report when the agent emits only its report; the
    # structured effects happened live (disk + CLI), so there is nothing else to parse.
    update = parse_ground_update("  Set strategy; recommended R-1 next.  ")

    assert update.report == "Set strategy; recommended R-1 next."
    assert update.artifact_refs == ()


def test_parse_ground_update_keeps_last_explicit_report_block() -> None:
    text = """Let me inspect the workspace.
Done. Here's the run-local report.
## Run-local report
old
Done. Here's the run-local report.
## Run-local report
new
"""

    update = parse_ground_update(text)

    assert update.report == "## Run-local report\nnew"
