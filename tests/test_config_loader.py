"""Config layer: parse config.yaml, build harnesses, run a round end-to-end."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from archon_horizon.config.harnesses import HarnessRegistry, UnknownHarnessKind
from archon_horizon.config.env import load_env_file
from archon_horizon.config.loader import build_orchestrator, build_workspace, load_config
from archon_horizon.config.schema import HarnessConfig
from archon_horizon.core.sessions import Focus, RunRecord
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet


def _queue_task(orch, task_id: str = "R-1", project: str = "ag-main") -> None:
    """Seed a human-created queued task (the roadmap no longer auto-creates one)."""
    orch.task_store.put(HorizonTask(
        id=task_id, project=project, objective="x", title="x",
        projects=(project,), status=TaskStatus.QUEUED, write_set=WriteSet(projects=(project,)),
    ))


def _completing_horizon(holder: dict):
    """A horizon harness callback that records `done` for the running task, exactly
    as a real agent does with `horizon task set <id> --status done` — the only way
    a task becomes terminal now (the report is never parsed for completion). The
    task store is read from ``holder['store']`` at call time, since it only exists
    after ``build_orchestrator``."""
    import dataclasses

    def record(req):
        store = holder["store"]
        for task in store.list():
            if task.status is TaskStatus.RUNNING:
                store.put(dataclasses.replace(task, status=TaskStatus.DONE))
                store.append_history(task.id, {
                    "at": "2026-07-02T00:00:00+00:00", "actor": "horizon", "field": "status",
                    "from": "running", "to": "done", "note": "agent recorded done via CLI",
                })
        return HarnessResult(ok=True, text="## Summary\nDid the work.")

    return record
from archon_horizon.harnesses.base import HarnessResult
from archon_horizon.harnesses.command import CommandHarness
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.transcript.model import TranscriptKind

CONFIG = """
workspace:
  name: ag-horizon
  state_dir: .archon-horizon
  rounds: 1
  ground_agent:
    harness: ground-default
  horizon_agent:
    harness: horizon-default
  scheduler:
    max_parallel_sessions: 2
harnesses:
  ground-default:
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


def test_project_dependencies_are_loaded(tmp_path: Path) -> None:
    body = CONFIG + """
  shared:
    path: projects/shared
  consumer:
    path: projects/consumer
    depends_on: [shared]
"""
    _write_config(tmp_path, body)

    workspace = build_workspace(load_config(tmp_path), tmp_path)

    assert workspace.project("consumer").depends_on == ("shared",)


def test_reference_transcription_config_defaults_and_overrides(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.reference_transcription.harness is None
    assert cfg.reference_transcription_model_name is None
    assert cfg.reference_transcription_harness_name == "ground-default"

    body = CONFIG.replace(
        "projects:\n",
        "references:\n"
        "  transcription:\n"
        "    harness: cheap-vision\n"
        "    model: flash-transcriber\n"
        "harnesses:\n"
        "  cheap-vision:\n"
        "    kind: null\n"
        "    model: default-cheap\n"
        "projects:\n",
    )
    overridden = tmp_path / "overridden"
    overridden.mkdir()
    _write_config(overridden, body)
    cfg2 = load_config(overridden)
    assert cfg2.reference_transcription_harness_name == "cheap-vision"
    assert cfg2.reference_transcription_model_name == "flash-transcriber"


def test_external_libraries_parsed_and_resolved(tmp_path: Path) -> None:
    body = CONFIG.replace(
        "projects:\n",
        "external_libraries:\n"
        "  - name: mathlib\n"
        "    rev: v4.20.0\n"
        "  - name: my-lib\n"
        "    github: owner/my-lib\n"
        "    rev: main\n"
        "  - acme/widgets@v1.2\n"
        "projects:\n",
    )
    _write_config(tmp_path, body)

    cfg = load_config(tmp_path)
    libs = {lib.name: lib for lib in cfg.external_libraries}

    assert set(libs) == {"mathlib", "my-lib", "acme/widgets"}
    assert libs["mathlib"].rev == "v4.20.0"
    assert libs["mathlib"].git_url == "https://github.com/leanprover-community/mathlib4.git"
    assert libs["my-lib"].git_url == "https://github.com/owner/my-lib.git"
    # String shorthand "owner/repo@rev" splits the rev and defaults to GitHub.
    assert libs["acme/widgets"].rev == "v1.2"
    assert libs["acme/widgets"].git_url == "https://github.com/acme/widgets.git"


def test_external_library_manifest_mismatch_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = CONFIG.replace(
        "projects:\n",
        "external_libraries:\n  - name: mathlib\n    rev: v4.20.0\nprojects:\n",
    )
    _write_config(tmp_path, body)
    (tmp_path / "projects" / "ag-main" / "lake-manifest.json").write_text(
        '{"packages": [{"name": "mathlib", "rev": "deadbeef"}]}', "utf-8"
    )

    cfg = load_config(tmp_path)

    captured = capsys.readouterr()
    assert cfg.external_libraries[0].rev == "v4.20.0"
    assert "'mathlib' is pinned to 'deadbeef'" in captured.out
    assert "external_libraries declares 'v4.20.0'" in captured.out


def test_registry_builds_codex_argv(tmp_path: Path) -> None:
    _write_config(tmp_path)
    cfg = load_config(tmp_path)
    harness = HarnessRegistry().build(cfg.harnesses["horizon-default"])
    assert isinstance(harness, CommandHarness)
    argv = harness._argv("PROMPT")  # noqa: SLF001 — asserting the wiring
    assert argv[:2] == ["codex", "exec"]
    assert "fable5" in argv and "model_reasoning_effort=high" in argv
    assert argv[-1] == "PROMPT"


def test_registry_maps_claude_named_effort_to_effort_flag() -> None:
    # A named tier drives claude's native --effort flag (not a thinking budget).
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "max"})
    harness = HarnessRegistry().build(cfg)
    argv = harness._argv("PROMPT")  # noqa: SLF001 — asserting the wiring
    assert argv[argv.index("--effort") + 1] == "max"
    assert "MAX_THINKING_TOKENS" not in harness.env_overrides


def test_registry_claude_ultracode_uses_settings_not_effort() -> None:
    # 'ultracode' is a session setting, not a --effort value: it is enabled via
    # `--settings '{"ultracode": true}'` (works headlessly under `claude -p`) and
    # sends no --effort flag (the setting already implies xhigh).
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "ultracode"})
    harness = HarnessRegistry().build(cfg)
    argv = harness._argv("PROMPT")  # noqa: SLF001
    assert "--effort" not in argv
    assert "--settings" in argv
    assert json.loads(argv[argv.index("--settings") + 1]) == {"ultracode": True}
    # It still surfaces to the Logs/UI as the effort label.
    assert getattr(harness, "horizon_effort") == "ultracode"


def test_registry_claude_effort_raw_integer_is_thinking_budget() -> None:
    # A raw integer is an explicit MAX_THINKING_TOKENS budget, not an --effort level.
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "9000"})
    harness = HarnessRegistry().build(cfg)
    assert harness.env_overrides["MAX_THINKING_TOKENS"] == "9000"
    assert "--effort" not in harness._argv("PROMPT")  # noqa: SLF001


def test_registry_claude_explicit_thinking_env_wins_over_budget() -> None:
    # An explicit MAX_THINKING_TOKENS in env is honoured; a named tier still uses --effort.
    cfg = HarnessConfig(
        name="horizon",
        kind="claude-code",
        model="opus",
        options={"effort": "low", "env": {"MAX_THINKING_TOKENS": "50000"}},
    )
    harness = HarnessRegistry().build(cfg)
    assert harness.env_overrides["MAX_THINKING_TOKENS"] == "50000"
    argv = harness._argv("PROMPT")  # noqa: SLF001
    assert argv[argv.index("--effort") + 1] == "low"


def test_registry_claude_effort_default_is_no_override() -> None:
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "default"})
    harness = HarnessRegistry().build(cfg)
    assert "MAX_THINKING_TOKENS" not in harness.env_overrides
    assert "--effort" not in harness._argv("PROMPT")  # noqa: SLF001
    assert getattr(harness, "horizon_effort", None) is None


def test_registry_codex_effort_default_omits_reasoning_flag() -> None:
    cfg = HarnessConfig(name="horizon", kind="codex", model="fable5", options={"effort": "default"})
    harness = HarnessRegistry().build(cfg)
    argv = harness._argv("PROMPT")  # noqa: SLF001 — asserting the wiring
    assert not any("model_reasoning_effort" in a for a in argv)


def test_registry_claude_unknown_effort_is_explicit() -> None:
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "bogus"})
    with pytest.raises(ValueError, match="unknown effort"):
        HarnessRegistry().build(cfg)


def test_registry_applies_optional_pricing_to_codex_parser() -> None:
    cfg = HarnessConfig(
        name="horizon",
        kind="codex",
        model="fable5",
        options={
            "pricing": {
                "input_per_million_usd": 10,
                "cached_input_per_million_usd": 1,
                "output_per_million_usd": 20,
            }
        },
    )
    harness = HarnessRegistry().build(cfg)
    assert isinstance(harness, CommandHarness)
    [event] = harness._parser(  # noqa: SLF001 — asserting config-to-parser wiring
        '{"type":"turn.completed","usage":{"input_tokens":1000000,'
        '"cached_input_tokens":250000,"output_tokens":500000}}'
    )
    assert event.kind is TranscriptKind.USAGE
    assert event.usage is not None
    assert event.usage.cost_usd == 17.75
    assert event.data["cost_estimated"] is True


def test_registry_builds_claude_provider_alias_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOONSHOT_API_KEY", "kimi-secret")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = HarnessConfig(name="kimi", kind="claude-code", model="kimi")

    harness = HarnessRegistry().build(cfg)

    assert isinstance(harness, CommandHarness)
    argv = harness._argv("PROMPT")  # noqa: SLF001
    assert argv[:2] == ["claude", "-p"]
    assert "kimi-k2.6" in argv
    assert harness.env_overrides["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert harness.env_overrides["ANTHROPIC_AUTH_TOKEN"] == "kimi-secret"


def test_registry_builds_deepseek_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    cfg = HarnessConfig(name="deepseek", kind="claude-code", model="deepseek")

    harness = HarnessRegistry().build(cfg)

    assert isinstance(harness, CommandHarness)
    assert "deepseek-coder" in harness._argv("PROMPT")  # noqa: SLF001
    assert harness.env_overrides["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert harness.env_overrides["ANTHROPIC_AUTH_TOKEN"] == "deepseek-secret"


def test_registry_routes_provider_alias_through_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    cfg = HarnessConfig(name="deepseek", kind="claude-code", model="deepseek")

    harness = HarnessRegistry().build(cfg)

    assert isinstance(harness, CommandHarness)
    assert "deepseek/deepseek-r1" in harness._argv("PROMPT")  # noqa: SLF001
    assert harness.env_overrides["ANTHROPIC_API_KEY"] == ""
    assert harness.env_overrides["ANTHROPIC_AUTH_TOKEN"] == "or-secret"


def test_registry_provider_alias_missing_key_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MOONSHOT_API_KEY", raising=False)
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ValueError, match="MOONSHOT_API_KEY, KIMI_API_KEY"):
        HarnessRegistry().build(HarnessConfig(name="kimi", kind="claude-code", model="kimi"))


def test_registry_builds_claude_p_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("archon_horizon.config.harnesses._claude_p_supports", lambda _flag: False)
    cfg = HarnessConfig(
        name="claude-p",
        kind="claude-code",
        model="sonnet",
        options={"backend": "claude-p", "config_dir": "/tmp/claude-work"},
    )

    harness = HarnessRegistry().build(cfg)

    assert isinstance(harness, CommandHarness)
    argv = harness._argv("PROMPT")  # noqa: SLF001
    assert argv[0] == "claude-p"
    assert argv[1] == "PROMPT"
    assert "--timeout-sec" in argv
    assert harness.env_overrides["CLAUDE_CONFIG_DIR"] == "/tmp/claude-work"


def test_registry_warns_when_claude_p_is_missing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("archon_horizon.config.harnesses.shutil.which", lambda _binary: None)
    monkeypatch.setattr("archon_horizon.config.harnesses._claude_p_supports", lambda _flag: False)

    HarnessRegistry().build(
        HarnessConfig(name="claude-p", kind="claude-code", options={"backend": "claude-p"})
    )

    captured = capsys.readouterr()
    assert "uv tool install --force git+https://github.com/AxelDlv00/claude-p" in captured.out


def test_load_env_file_keeps_shell_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("DEEPSEEK_API_KEY=file-secret\nMOONSHOT_MODEL=kimi-custom\n", "utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "shell-secret")
    monkeypatch.delenv("MOONSHOT_MODEL", raising=False)

    loaded = load_env_file(tmp_path)

    assert loaded["DEEPSEEK_API_KEY"] == "file-secret"
    assert loaded["MOONSHOT_MODEL"] == "kimi-custom"
    assert os.environ["DEEPSEEK_API_KEY"] == "shell-secret"
    assert os.environ["MOONSHOT_MODEL"] == "kimi-custom"


def test_unknown_kind_is_explicit(tmp_path: Path) -> None:
    with pytest.raises(UnknownHarnessKind):
        HarnessRegistry().build(HarnessConfig(name="x", kind="does-not-exist"))


def test_build_orchestrator_runs_with_harness_overrides(tmp_path: Path) -> None:
    from archon_horizon.transcript.sink import read_transcript

    _write_config(tmp_path)
    holder: dict = {}
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        # "done" is the agent's word: it records the status itself via the CLI for
        # the task to reach DONE (a clean exit alone leaves it QUEUED).
        "horizon-default": NullHarness(_completing_horizon(holder)),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    holder["store"] = orch.task_store
    orch.start_with = "ground"  # this test asserts the opening-Ground (G/H/G) layout
    _queue_task(orch)
    reports = orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert reports[0].tasks_run == ("R-1",)
    assert orch.task_store.get("R-1").status is TaskStatus.DONE

    # The run claimed a numbered log dir with ordered sessions + a transcript.
    run_dir = tmp_path / ".archon-horizon" / "runs" / "0001"
    assert (run_dir / "run.yaml").exists()
    sessions = sorted((run_dir / "sessions").iterdir())
    assert sessions[0].name == "0001-ground"
    assert any((s / "transcript.jsonl").exists() for s in sessions)
    assert (sessions[0] / "report.md").exists()
    assert any("horizon-R-1" in s.name for s in sessions)
    system = next(s for s in sessions if s.name.endswith("-system"))
    system_report = (system / "report.md").read_text("utf-8")
    assert "## Checklist" in system_report
    assert "## Issues" in system_report
    assert "Report saved:" in system_report
    system_events = read_transcript(system / "transcript.jsonl")
    assert any("Report saved:" in event.text for event in system_events)
    # Flat alternation: a reconcile ground closes the G/H/G round, named uniformly.
    assert any(s.name.endswith("-ground") for s in sessions[2:])
    assert not (tmp_path / ".archon-horizon" / "reports").exists()


def test_resume_skips_finished_rounds_and_reruns_the_interrupted_one(tmp_path: Path) -> None:
    import shutil

    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        # Horizon fails, so R-1 stays retryable and round 1 has work to resume into.
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=False, text="boom")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    orch.start_with = "ground"  # exercise the opening-Ground (offset-1) resume layout
    _queue_task(orch)
    # Two rounds on a focused task (re-run each round). Each agent step is followed
    # by its own system session: [Ground, system, Horizon, system, Ground, system, …].
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))
    sessions_dir = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions"
    sessions = sorted(sessions_dir.iterdir())
    assert [s.name for s in sessions[:5]] == [
        "0001-ground", "0002-system", "0003-horizon-R-1", "0004-system", "0005-ground",
    ]
    # Simulate a crash during round 1: drop its sessions.
    for s in sessions[5:]:
        shutil.rmtree(s)

    reports = orch.run(orch.run_store.get("0001"), resume=True)

    # Round 0 finished, so it is skipped; resume then drives a fresh batch of
    # ``rounds_requested`` (2) Horizon rounds from the resume point — the interrupted
    # round 1 (re-run) plus round 2 — so the run keeps being pushed forward.
    assert [r.round_index for r in reports] == [1, 2]
    assert all(r.tasks_run == ("R-1",) for r in reports)
    # The opening ground was not re-run, and round 1's horizon was recreated.
    after = sorted(p.name for p in sessions_dir.iterdir())
    assert after[:5] == ["0001-ground", "0002-system", "0003-horizon-R-1", "0004-system", "0005-ground"]
    assert any("horizon-R-1" in name for name in after[5:])


def test_resume_of_cleanly_finished_run_runs_a_fresh_batch(tmp_path: Path) -> None:
    """Resuming a run that already completed all its rounds is not a no-op: it runs
    another full batch of forward rounds on the same focus, so ``--resume`` means
    "keep pushing this run" rather than silently finishing."""
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        # Horizon fails, so the focused task never reaches DONE and every round has
        # work — the run runs all its rounds to completion (no focus-complete stop).
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=False, text="boom")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    _queue_task(orch)

    first = orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))
    assert [r.round_index for r in first] == [0, 1]  # both planned rounds ran cleanly

    # Nothing was interrupted, yet resume must still drive a fresh batch — another
    # two rounds (the run's configured count), numbered after the finished ones.
    reports = orch.run(orch.run_store.get("0001"), resume=True)
    assert [r.round_index for r in reports] == [2, 3]
    assert all(r.tasks_run == ("R-1",) for r in reports)


def test_default_alternation_starts_on_horizon(tmp_path: Path) -> None:
    """The default run opens on Horizon and closes on Ground — H-G-H-G-…-G with no
    upfront planning Ground. Each agent step is followed by its own system session,
    so a 2-round run is [system, Horizon, system, Ground, system, Horizon, …]."""
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=False, text="boom")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))

    sessions_dir = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions"
    agents = [s.name for s in sorted(sessions_dir.iterdir()) if not s.name.endswith("-system")]
    # No opening Ground: the first agent session is a Horizon, then Grounds only
    # ever appear *after* a Horizon (H, G, H, G).
    assert agents == ["0002-horizon-R-1", "0004-ground", "0006-horizon-R-1", "0008-ground"]


def test_write_lock_conflict_warns_but_still_runs_horizon(tmp_path: Path) -> None:
    """The write lock is advisory: when a concurrent holder owns an overlapping
    write set, the Horizon still runs (it is no longer deferred) and a
    ``task.lock_warning`` is emitted instead of ``task.deferred``."""
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=True, text="done")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    _queue_task(orch)
    # Simulate another concurrent run holding the same project's write set.
    assert orch.locks.acquire("other-run", WriteSet(projects=("ag-main",)))

    reports = orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    assert reports[0].tasks_run == ("R-1",)  # ran despite the conflict, not deferred
    types = {e.type for e in orch.event_log.read_all()}
    assert "task.lock_warning" in types
    assert "task.deferred" not in types


def test_queue_focus_never_reopens_agent_declared_done(tmp_path: Path) -> None:
    """`done` is the agent's word and terminal: the orchestrator never reopens a
    done focused task — not mid-run, and not on an explicit human launch. Re-running
    a finished task is a deliberate act (`horizon task set <id> --status queued`),
    guarded at the CLI."""
    _write_config(tmp_path)
    orch = build_orchestrator(tmp_path, harnesses={
        "ground-default": NullHarness(""), "horizon-default": NullHarness("")})
    orch.task_store.put(HorizonTask(
        id="R-1", project="ag-main", objective="x", title="x",
        projects=("ag-main",), status=TaskStatus.DONE, write_set=WriteSet(projects=("ag-main",)),
    ))
    run = RunRecord(id="0001", focus=Focus(tasks=("R-1",)))

    # Mid-run (initial=False): the agent-declared DONE is respected, not reopened.
    orch._queue_focus_for_round(run, initial=False)
    assert orch.task_store.get("R-1").status is TaskStatus.DONE

    # Even an explicit launch (initial=True) does NOT reopen a done task.
    orch._queue_focus_for_round(run, initial=True)
    assert orch.task_store.get("R-1").status is TaskStatus.DONE


def test_resume_reconcile_recovery_does_not_consume_a_batch_round(tmp_path: Path) -> None:
    """When resume must first replay a dangling reconcile Ground (a Horizon finished
    but its Ground never ran), that recovery is extra — the run still gets a full
    batch of ``rounds_requested`` Horizon rounds afterwards."""
    import shutil

    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=False, text="boom")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    orch.start_with = "ground"
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))

    # Crash losing only round 1's reconcile Ground: drop everything after round 1's
    # Horizon so the last completed session is that Horizon (reconcile_only=True).
    sessions_dir = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions"
    survivors = sorted(sessions_dir.iterdir())
    last_horizon = [p for p in survivors if p.name.endswith("-horizon-R-1")][-1]
    for p in survivors:
        if p.name > last_horizon.name:
            shutil.rmtree(p)
    runlog = orch.run_logs.get("0001")
    assert orch._resume_point(runlog)[2] is True  # reconcile_only

    reports = orch.run(orch.run_store.get("0001"), resume=True)
    # The recovery Ground replays round 1's reconcile (no RoundReport), then a full
    # batch of 2 Horizon rounds runs — rounds 2 and 3, not just one.
    assert [r.round_index for r in reports] == [2, 3]


def test_resume_rounds_override_sets_the_fresh_batch_size(tmp_path: Path) -> None:
    """``--resume`` with ``--rounds N`` runs exactly N forward rounds from the
    resume point, overriding the run's configured count."""
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=False, text="boom")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))

    reports = orch.run(orch.run_store.get("0001"), resume=True, rounds_override=1)
    assert [r.round_index for r in reports] == [2]  # exactly one more round, not two


def test_resume_requeues_task_left_running_by_interrupted_horizon(tmp_path: Path) -> None:
    """A crash mid-Horizon leaves the task stuck in RUNNING. Resume must revive it.

    The scheduler only picks QUEUED tasks and an unfocused run has no focus to
    re-queue, so without reviving the interrupted task a ``run --resume`` selects
    nothing and stops with ``no-runnable-tasks`` — never continuing the work.
    """
    import json

    _write_config(tmp_path)
    holder: dict = {}
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        # Records `done` itself, so the revived task actually reaches DONE on resume.
        "horizon-default": NullHarness(_completing_horizon(holder)),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    holder["store"] = orch.task_store
    _queue_task(orch)
    # Unfocused run (the reported failure mode): no focus.tasks to lean on.
    orch.run(RunRecord(id="", focus=Focus(), rounds_requested=1))
    sessions_dir = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions"

    # Simulate a crash mid-Horizon: drop the reconcile Ground (and later system
    # sessions), strip the Horizon transcript's session_end so it reads as
    # interrupted, and leave the task stuck in RUNNING as a killed process would.
    sessions = sorted(sessions_dir.iterdir())
    horizon = next(s for s in sessions if s.name.endswith("-horizon-R-1"))
    for s in sessions:
        if s.name > horizon.name:
            import shutil

            shutil.rmtree(s)
    lines = horizon.joinpath("transcript.jsonl").read_text("utf-8").splitlines()
    horizon.joinpath("transcript.jsonl").write_text(
        "\n".join(l for l in lines if json.loads(l).get("kind") != "session_end") + "\n", "utf-8"
    )
    running = dataclasses_replace_status(orch, "R-1", TaskStatus.RUNNING)
    assert running.status is TaskStatus.RUNNING

    reports = orch.run(orch.run_store.get("0001"), resume=True)

    assert [r.round_index for r in reports] == [0]
    assert reports[0].tasks_run == ("R-1",)  # revived and re-run, not a no-op
    assert orch.task_store.get("R-1").status is TaskStatus.DONE


def dataclasses_replace_status(orch, task_id: str, status: TaskStatus):
    import dataclasses

    task = orch.task_store.get(task_id)
    return orch.task_store.put(dataclasses.replace(task, status=status))


def test_recover_horizon_result_reads_the_horizon_not_system_session(tmp_path: Path) -> None:
    """Resume's reconcile path recovers the prior Horizon result by index.

    System sessions are interleaved on disk (``[ground, system, horizon, …]``)
    but excluded from the ``2i+1`` index scheme. Recovering round 0 must return
    the Horizon session's data (task R-1), not the system session that sits at
    the same unfiltered index — the regression guarded here.
    """
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=False, text="boom")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    orch.start_with = "ground"  # opening Ground → offset-1 layout this test targets
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))
    runlog = orch.run_logs.get("0001")

    # offset=1: an opening Ground sits at filtered index 0, so round 0's Horizon
    # is at 2*0+1 (see _resume_point/_opening_offset).
    recovered = orch._recover_horizon_result(runlog, 0, 1)
    assert recovered is not None
    assert recovered.task_id == "R-1"          # the horizon session, not "" from -system
    # A crashed (ok=False) session records no terminal status, so the machine
    # returns the task to queued; the recovered session meta carries that.
    assert recovered.status is TaskStatus.QUEUED
    assert "## Checklist" not in recovered.report  # i.e. not the system session's report

    # Degenerate layout guard: if the 2i+offset math lands on a non-Horizon
    # session (here the opening Ground, whose session status is "ok" — not a
    # TaskStatus), recover nothing instead of raising ``TaskStatus('ok')``. This
    # is what a run recorded with only Grounds (older lock-skip builds) hit on
    # resume.
    assert orch._recover_horizon_result(runlog, 0, 0) is None


def _fake_completed_session(runlog, label: str, *, role: str) -> None:
    """Create a runlog session that reads as complete (carries a session_end)."""
    from archon_horizon.transcript.sink import JsonlTranscriptSink
    from archon_horizon.transcript.model import TranscriptEvent

    session = runlog.new_session(label)
    sink = JsonlTranscriptSink(session.transcript_path)
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"role": role}))
    sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": True}))
    session.write_meta({"role": role, "status": "ok"})


def test_resume_point_reconcile_only_when_last_session_is_a_horizon(tmp_path: Path) -> None:
    """A run interrupted right after a Horizon completed (its reconcile Ground never
    ran) resumes by replaying just that Ground — reconcile_only=True."""
    _write_config(tmp_path)
    orch = build_orchestrator(tmp_path, harnesses={
        "ground-default": NullHarness(""), "horizon-default": NullHarness("")})
    orch.start_with = "ground"
    runlog = orch.run_logs.allocate()
    _fake_completed_session(runlog, "ground", role="ground")         # opening Ground
    _fake_completed_session(runlog, "horizon-R-1", role="horizon")   # Horizon, no reconcile Ground

    assert orch._resume_point(runlog) == (False, 0, True)


def test_resume_point_degenerate_all_ground_layout_resumes_into_horizon(tmp_path: Path) -> None:
    """A legacy run recorded with only Grounds (older builds that skipped Horizon on
    a lock conflict) has an odd completed-session count yet ends on a Ground. It must
    resume into a fresh Horizon (reconcile_only=False), not a bare reconcile Ground —
    the parity-based check used to get this wrong and crash on resume."""
    _write_config(tmp_path)
    orch = build_orchestrator(tmp_path, harnesses={
        "ground-default": NullHarness(""), "horizon-default": NullHarness("")})
    orch.start_with = "ground"
    runlog = orch.run_logs.allocate()
    for _ in range(6):  # opening Ground + 5 more Grounds, no Horizons (like real run 0012)
        _fake_completed_session(runlog, "ground", role="ground")

    run_opening, resume_round, reconcile_only = orch._resume_point(runlog)
    assert reconcile_only is False  # ends on a Ground → next step is a Horizon, not a Ground


def test_resume_offset_zero_when_no_opening_ground(tmp_path: Path) -> None:
    """A ``start_with='horizon'`` run has no opening Ground, so a system session is
    flushed first and the first agent session on disk is round-0's Horizon. The
    resume math must shift down by one (offset 0); otherwise it mistakes the
    Horizon for the opening Ground and skips/misaligns the interrupted agent.
    """
    import shutil

    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=True, text="done")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    orch.start_with = "horizon"
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))

    sessions_dir = tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions"
    names = sorted(p.name for p in sessions_dir.iterdir())
    # No opening Ground: a system flush leads, then the round-0 Horizon.
    assert names[0].endswith("-system")
    assert "horizon-R-1" in names[1]

    runlog = orch.run_logs.get("0001")
    agents = orch._agent_sessions(runlog)
    assert orch._is_ground_session(agents[0]) is False
    assert orch._opening_offset(agents) == 0
    # Both rounds finished → resume starts at the next round (2), not round 1.
    assert orch._resume_point(runlog) == (False, 2, False)

    # Simulate a crash losing round 1: drop everything after round 0's reconcile
    # Ground. Resume must re-run round 1 (offset keeps the indices aligned).
    survivors = sorted(sessions_dir.iterdir())
    keep = [p for p in survivors if p.name.endswith("-ground")][0]  # 0004-ground
    for p in survivors:
        if p.name > keep.name:
            shutil.rmtree(p)
    runlog = orch.run_logs.get("0001")
    assert orch._opening_offset(orch._agent_sessions(runlog)) == 0
    assert orch._resume_point(runlog) == (False, 1, False)

    reports = orch.run(orch.run_store.get("0001"), resume=True)
    # Fresh batch of ``rounds_requested`` (2) from the resume point: round 1 (the
    # dropped round) plus round 2.
    assert [r.round_index for r in reports] == [1, 2]
    assert all(r.tasks_run == ("R-1",) for r in reports)


def test_resume_run_id_zero_pads_bare_number(tmp_path: Path) -> None:
    """`--resume 3` must normalize to the zero-padded on-disk id `0003`, matching
    the store lookup and the "Resuming run …" display."""
    from archon_horizon.commands.run import RunCommand

    _write_config(tmp_path)
    orch = build_orchestrator(tmp_path, harnesses={
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=True, text="done")),
    })
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))

    cmd = RunCommand.__new__(RunCommand)
    cmd.resume = "1"  # bare number, not the padded "0001"
    run = cmd._resume_run(orch)
    assert run.id == "0001"


def test_subagent_harness_override_defaults_to_ground(tmp_path: Path) -> None:
    body = CONFIG.replace(
        "  ground_agent:\n    harness: ground-default\n",
        "  ground_agent:\n    harness: ground-default\n    subagent_harness: cheap\n",
    ).replace(
        "harnesses:\n",
        'harnesses:\n  cheap:\n    kind: "null"\n',
    )
    _write_config(tmp_path, body)
    overrides = {
        "ground-default": NullHarness("g"),
        "horizon-default": NullHarness("h"),
        "cheap": NullHarness("c"),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    assert orch.ground_subagents  # builtin descriptors are loaded
    assert all(sub.harness is overrides["cheap"] for sub in orch.ground_subagents)

    # Without the override, subagents fall back to the Ground harness.
    plain = tmp_path / "plain"
    plain.mkdir()
    _write_config(plain, CONFIG)
    orch2 = build_orchestrator(plain, harnesses=overrides)
    assert all(sub.harness is overrides["ground-default"] for sub in orch2.ground_subagents)


def test_roles_parse_defaults_and_filters() -> None:
    from archon_horizon.config.schema import _parse_roles

    assert _parse_roles(None) == ("ground", "horizon")          # unset -> both
    assert _parse_roles([]) == ("ground", "horizon")            # empty -> both
    assert _parse_roles(["bogus"]) == ("ground", "horizon")     # unknown -> both
    assert _parse_roles(["horizon"]) == ("horizon",)
    assert _parse_roles(["ground"]) == ("ground",)
    assert _parse_roles("horizon") == ("horizon",)              # bare string
    assert _parse_roles(["Horizon", "GROUND"]) == ("horizon", "ground")  # case-insensitive
    assert _parse_roles(["horizon", "ground", "horizon"]) == ("horizon", "ground")  # dedup, ordered


def test_roles_parsed_from_workspace_config(tmp_path: Path) -> None:
    body = CONFIG.replace("  rounds: 1\n", "  rounds: 1\n  roles: [horizon]\n")
    _write_config(tmp_path, body)
    cfg = load_config(tmp_path)
    assert cfg.roles == ("horizon",)


def test_horizon_only_loop_runs_no_ground(tmp_path: Path) -> None:
    # roles: [horizon] drives a Horizon-only loop: a Horizon step each round, and no
    # Ground session at all (neither opening nor reconcile).
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("ground must not run"),
        # Records no terminal status, so R-1 stays queued and re-runs each round.
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=True, text="wip")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    orch.roles = ("horizon",)
    _queue_task(orch)

    reports = orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=2))

    assert [r.round_index for r in reports] == [0, 1]
    assert all(r.tasks_run == ("R-1",) for r in reports)
    names = [s.name for s in sorted((tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions").iterdir())]
    assert any("horizon-R-1" in n for n in names)
    assert not any(n.endswith("-ground") for n in names)  # no Ground ever


def test_ground_only_loop_runs_no_horizon(tmp_path: Path) -> None:
    # roles: [ground] drives a Ground-only loop: one standalone Ground session per
    # round, and no Horizon step (no task selection).
    _write_config(tmp_path)
    overrides = {
        "ground-default": NullHarness("organized the workspace"),
        "horizon-default": NullHarness("horizon must not run"),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
    orch.roles = ("ground",)
    _queue_task(orch)

    reports = orch.run(RunRecord(id="", rounds_requested=2))

    assert [r.round_index for r in reports] == [0, 1]
    names = [s.name for s in sorted((tmp_path / ".archon-horizon" / "runs" / "0001" / "sessions").iterdir())]
    assert len([n for n in names if n.endswith("-ground")]) == 2  # one Ground per round
    assert not any("horizon" in n for n in names)
    # The queued task is never touched (no Horizon selected it).
    assert orch.task_store.get("R-1").status is TaskStatus.QUEUED
