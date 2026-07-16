"""Agent prompt/output contract: prompts expose schema, parser consumes it."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.agents.base import HorizonContext
from archon_horizon.agents.prompts import compose_horizon_prompt
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
    assert "Task focus" in prompt
    assert "you choose the strategy from the live Lean state" in prompt
    assert "write local helper scripts/tools" in prompt
    assert "## Progress" in prompt
    assert "4 sorries -> 3 sorries" in prompt
    assert "inline `-` bullets" in prompt
    assert "## Why I stopped" in prompt
    # Horizon owns its task status and records `done` via the CLI when fully complete.
    assert "fully complete" in prompt
    assert "task set <task_id> --status done" in prompt
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


def test_prompt_body_loads_from_bundled_default(tmp_path: Path) -> None:
    # With no workspace override, the composer falls back to the bundled md bodies.
    from archon_horizon.agents.prompts import bundled_prompt_names

    assert set(bundled_prompt_names()) == {"horizon"}
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=HorizonTask(id="T-1", project="ag-main", objective="x", write_set=WriteSet()),
        roadmap=Roadmap(),
    )
    assert "You are Archon Horizon's Horizon agent" in compose_horizon_prompt(ctx)


def test_workspace_prompt_override_wins(tmp_path: Path) -> None:
    # A workspace copy at <state_dir>/prompts/<name>.md overrides the bundled body,
    # so a human can retune the agent's instructions without touching code.
    ws = _workspace(tmp_path)
    override_dir = ws.state_path / "prompts"
    override_dir.mkdir(parents=True)
    (override_dir / "horizon.md").write_text("CUSTOM HORIZON ROLE PROSE.", "utf-8")

    ctx = HorizonContext(
        workspace=ws,
        run=RunRecord(id="S-1", rounds_requested=1),
        task=HorizonTask(id="T-1", project="ag-main", objective="Prove Foo.bar", write_set=WriteSet()),
        roadmap=Roadmap(),
    )
    prompt = compose_horizon_prompt(ctx)
    assert "CUSTOM HORIZON ROLE PROSE." in prompt
    assert "You are Archon Horizon's Horizon agent" not in prompt  # bundled body replaced
    # Dynamic sections are still injected around the custom body.
    assert "Prove Foo.bar" in prompt
    assert "# Skills" in prompt


def test_install_prompts_writes_editable_copies(tmp_path: Path) -> None:
    from archon_horizon.agents.prompts import install_prompts

    written = install_prompts(tmp_path)
    assert set(written) == {"horizon"}
    assert (tmp_path / ".archon-horizon" / "prompts" / "horizon.md").is_file()
    # Re-installing over identical copies is a no-op.
    assert install_prompts(tmp_path) == []
