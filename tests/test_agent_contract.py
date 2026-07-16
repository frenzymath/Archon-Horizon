"""Agent prompt contract: a task directive + "load the `horizon` skill".

The prompt pushes no role prose or workspace state — the skill is the contract
and state is pulled through the CLI — so these tests pin (a) the small
directive shape and (b) that the load-bearing conventions actually live in the
packaged skill files.
"""

from __future__ import annotations

from pathlib import Path

from archon_horizon.agents.base import HorizonContext
from archon_horizon.agents.prompts import horizon_task_prompt
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.core.workspace import Project, Workspace

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "src" / "archon_horizon" / "skills"


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        name="ws",
        root=tmp_path,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"), build_command="lake build")},
    )


def test_horizon_prompt_is_directive_plus_skill(tmp_path: Path) -> None:
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

    prompt = horizon_task_prompt(ctx)

    # The directive: skill pointer + task identity + one-shot framing.
    assert "`horizon`" in prompt and "skill" in prompt
    assert "T-1" in prompt
    assert "Prove Foo.bar" in prompt
    assert "roadmap=R-1" in prompt
    assert "ag-main" in prompt
    assert "one-shot" in prompt
    # No pushed policy/state: those moved to the skill / the CLI (pull).
    assert "# Roadmap" not in prompt
    assert "# Memory" not in prompt
    assert "# Subagents" not in prompt
    assert len(prompt) < 2000


def test_horizon_skill_carries_the_load_bearing_conventions() -> None:
    # What the old composed prompt pushed must survive in the packaged skill.
    skill = (_SKILLS_DIR / "horizon" / "SKILL.md").read_text("utf-8")
    assert "## Progress" in skill and "## Why I stopped" in skill  # report shape
    assert "--status done" in skill                                # status ownership
    assert "FULLY complete" in skill
    assert "foreground" in skill                                   # one-shot discipline
    subagents = (_SKILLS_DIR / "subagents" / "SKILL.md").read_text("utf-8")
    assert "model" in subagents and "cheap" in subagents           # model economy


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

    # Native resume: continue preamble PLUS the full prompt (so the agent still has
    # its instructions if the engine's native replay didn't restore them), and the
    # engine session id passed through.
    assert harness.request.resume_session_id == "sid-1"
    assert harness.request.prompt.startswith(HORIZON_CONTINUE)
    assert "Prove Foo.bar" in harness.request.prompt


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


class _ScriptedHarness:
    """Harness double that returns a scripted result per call and records every
    request it was handed (so a retry can be asserted)."""

    def __init__(self, results: list) -> None:
        from archon_horizon.harnesses.base import HarnessCapability

        self.capabilities = frozenset({HarnessCapability.RESUME})
        self._results = list(results)
        self.requests: list = []

    def run(self, request):
        self.requests.append(request)
        return self._results.pop(0)


def test_horizon_retries_fresh_when_native_resume_cannot_start(tmp_path: Path) -> None:
    from archon_horizon.agents.harness_agents import HORIZON_CONTINUE, HarnessHorizonAgent
    from archon_horizon.harnesses.base import HarnessResult

    task = HorizonTask(id="T-1", project="ag-main", objective="Prove Foo.bar")
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=task,
        roadmap=Roadmap(),
        resume_session_id="sid-gone",
    )

    # First call (the resume) aborts instantly; the fallback re-runs fresh.
    aborted = HarnessResult(ok=False, text="No conversation found with session ID: sid-gone",
                            metadata={"failure_reason": "aborted_early"})
    harness = _ScriptedHarness([aborted, HarnessResult(ok=True, text="done")])
    result = HarnessHorizonAgent(harness).run_task(ctx)

    assert len(harness.requests) == 2
    # First attempt: native resume with the continue preamble + full prompt.
    assert harness.requests[0].resume_session_id == "sid-gone"
    assert harness.requests[0].prompt.startswith(HORIZON_CONTINUE)
    # Fallback attempt: no resume id, full composed prompt (no preamble), and it succeeds.
    assert harness.requests[1].resume_session_id is None
    assert "Prove Foo.bar" in harness.requests[1].prompt
    assert result.status.value == "done"


def test_horizon_does_not_retry_when_resumed_session_did_work(tmp_path: Path) -> None:
    from archon_horizon.agents.harness_agents import HarnessHorizonAgent
    from archon_horizon.harnesses.base import HarnessResult

    task = HorizonTask(id="T-1", project="ag-main", objective="Prove Foo.bar")
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=task,
        roadmap=Roadmap(),
        resume_session_id="sid-1",
    )

    # A resumed session that ran and then failed (not an instant abort) must not
    # be re-run fresh — that would discard its real progress.
    failed = HarnessResult(ok=False, text="proof broke", metadata={"failure_reason": "task-failed"})
    harness = _ScriptedHarness([failed])
    HarnessHorizonAgent(harness).run_task(ctx)

    assert len(harness.requests) == 1  # no fallback retry


def test_workspace_wide_task_prompt_names_no_single_project(tmp_path: Path) -> None:
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=HorizonTask(id="T-2", project="", objective="tidy", write_set=WriteSet()),
        roadmap=Roadmap(),
    )
    prompt = horizon_task_prompt(ctx)
    assert "workspace-wide" in prompt
