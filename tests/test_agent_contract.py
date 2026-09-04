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
    )

    prompt = horizon_task_prompt(ctx)

    # The directive: skill pointer + task identity + one-shot framing.
    assert "`horizon`" in prompt and "skill" in prompt
    assert "T-1" in prompt
    assert "Prove Foo.bar" in prompt
    assert "roadmap=R-1" in prompt
    assert "ag-main" in prompt
    assert "one-shot" in prompt
    # No full policy/state dump: those remain in the skill / CLI. A stale
    # workspace does receive a compact current-version safety guard.
    assert "# Roadmap" not in prompt
    assert "# Memory" not in prompt
    assert "# Subagents" not in prompt
    assert "installed workspace skills are stale" in prompt
    assert "lean-check" in prompt and "janitor" in prompt
    assert "honesty-reviewer" in prompt
    assert len(prompt) < 2000


def test_horizon_skill_carries_the_load_bearing_conventions() -> None:
    # What the old composed prompt pushed must survive in the packaged skill.
    skill = (_SKILLS_DIR / "horizon" / "SKILL.md").read_text("utf-8")
    assert "## Progress" in skill and "## Why I stopped" in skill  # report shape
    assert "--status done" in skill                                # status ownership
    assert "FULLY complete" in skill
    assert "foreground" in skill                                   # one-shot discipline
    subagents = (_SKILLS_DIR / "subagents" / "SKILL.md").read_text("utf-8")
    assert "model" in subagents and "lighter capable" in subagents  # dispatcher-owned model economy
    assert "ground" in skill and "before marking" in skill          # optional fresh-context signal
    assert "honesty-reviewer" in skill and "first unmet producer" in skill
    assert "Claim class" in skill and "Frontier before/after" in skill
    assert "boundary-maintenance" in skill and "archive or complete" in skill
    assert "Formalization note" in skill and "graph add comment" in skill
    assert "dispatch **`janitor`" in skill and "Collection-health warnings are a dispatch trigger" in skill
    assert "lean_diagnostic_messages" in skill and "after each subsequent edit" in skill
    assert "more than one file" in subagents and "consider dispatching at least one" in subagents
    assert "advisory signals" in subagents and "runtime requirements" in subagents

    lean_check = (_SKILLS_DIR / "lean-check" / "SKILL.md").read_text("utf-8")
    assert "Before the first proof edit" in lean_check
    assert "reserve `lake build` for the final session" in lean_check
    assert "LSP is sufficient between edits" in lean_check
    assert "module build after every proof" in lean_check
    assert ".codex/agents/*.toml" in subagents
    assert "empty roster" in subagents

    formalization_review = (_SKILLS_DIR / "formalization-review" / "SKILL.md").read_text("utf-8")
    assert "not a completion gate" in formalization_review
    assert "Source contract" in formalization_review
    assert "Representations and bridges" in formalization_review
    assert "Claim evidence" in formalization_review
    assert "#print axioms" in formalization_review
    assert "Across the lifecycle" in formalization_review
    assert "Fix / Context / Preserve" in formalization_review
    assert "source-discovery" in formalization_review
    mathlib = (_SKILLS_DIR / "mathlib-conventions" / "SKILL.md").read_text("utf-8")
    assert "consult [[formalization-review]]" in mathlib
    blueprint_conventions = (_SKILLS_DIR / "blueprint-conventions" / "SKILL.md").read_text("utf-8")
    assert "[[formalization-review]]" in blueprint_conventions
    assert "\\dcref" in blueprint_conventions
    assert "semantic and reader-facing" in blueprint_conventions
    assert "proof should contain ordinary mathematical prose" in blueprint_conventions
    assert "conventions below are Horizon's defaults and advice" in blueprint_conventions
    assert "proof-side `\\uses{...}` or `\\proves{...}`" in blueprint_conventions
    blueprint_integrity = (_SKILLS_DIR / "blueprint-integrity" / "SKILL.md").read_text("utf-8")
    assert "local `\\label` ids are stable and preferably semantic" in blueprint_integrity
    assert "Check macro placement" in blueprint_integrity
    assert "conventions are advisory" in blueprint_integrity
    assert "project-supported" in blueprint_integrity


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
    )
    prompt = horizon_task_prompt(ctx)
    assert "workspace-wide" in prompt


def test_agent_env_carries_full_session_identity(tmp_path: Path) -> None:
    # The env is the engine-agnostic context channel: run/round/session/task are
    # exported so Claude Code and Codex sessions can read their own identity.
    from archon_horizon.agents.harness_agents import HarnessHorizonAgent

    task = HorizonTask(id="T-9", project="ag-main", title="Prove the crux", objective="x")
    log_dir = tmp_path / "runs" / "0004" / "sessions" / "0002-horizon-T-9"
    log_dir.mkdir(parents=True)
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="0004", rounds_requested=3),
        task=task,
        log_dir=log_dir,
        round_index=1,
        rounds_total=3,
    )
    harness = _RecordingHarness(resume=False)
    HarnessHorizonAgent(harness).run_task(ctx)

    env = harness.request.metadata["env"]
    assert env["ARCHON_HORIZON_INTERACTIVE"] == "0"
    assert env["ARCHON_HORIZON_RUN"] == "0004"
    assert env["ARCHON_HORIZON_SESSION"] == "0002-horizon-T-9"
    assert env["ARCHON_HORIZON_SESSION_DIR"] == str(log_dir.resolve())
    assert env["ARCHON_HORIZON_ROUND"] == "1"
    assert env["ARCHON_HORIZON_ROUNDS"] == "3"
    assert env["ARCHON_HORIZON_TASK"] == "T-9"
    assert env["ARCHON_HORIZON_TASK_TITLE"] == "Prove the crux"
    assert env["ARCHON_HORIZON_SKILL"] == str(
        (tmp_path / ".claude" / "skills" / "horizon" / "SKILL.md").resolve()
    )
    assert env["ARCHON_HORIZON_TMP"] == env["TMPDIR"] == env["TMP"] == env["TEMP"]
    assert env["ARCHON_HORIZON_TMP_ROOT"] == str((tmp_path / ".archon-horizon" / "tmp").resolve())
    # Session scratch is disposable and is reclaimed after the harness returns.
    assert not Path(env["ARCHON_HORIZON_TMP"]).exists()


def test_horizon_prompt_uses_absolute_skill_path(tmp_path: Path) -> None:
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=HorizonTask(id="T-1", project="ag-main", objective="x"),
    )

    prompt = horizon_task_prompt(ctx)

    assert str((tmp_path / ".claude/skills/horizon/SKILL.md").resolve()) in prompt
