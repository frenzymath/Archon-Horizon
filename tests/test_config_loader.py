"""Config layer: parse config.yaml, build harnesses, run a round end-to-end."""

from __future__ import annotations

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


def test_registry_maps_claude_effort_to_thinking_budget() -> None:
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "xhigh"})
    harness = HarnessRegistry().build(cfg)
    assert harness.env_overrides["MAX_THINKING_TOKENS"] == "32000"


def test_registry_claude_effort_accepts_raw_integer() -> None:
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "9000"})
    harness = HarnessRegistry().build(cfg)
    assert harness.env_overrides["MAX_THINKING_TOKENS"] == "9000"


def test_registry_claude_explicit_thinking_env_wins_over_effort() -> None:
    cfg = HarnessConfig(
        name="horizon",
        kind="claude-code",
        model="opus",
        options={"effort": "low", "env": {"MAX_THINKING_TOKENS": "50000"}},
    )
    harness = HarnessRegistry().build(cfg)
    assert harness.env_overrides["MAX_THINKING_TOKENS"] == "50000"


def test_registry_claude_effort_default_is_no_override() -> None:
    cfg = HarnessConfig(name="horizon", kind="claude-code", model="opus", options={"effort": "default"})
    harness = HarnessRegistry().build(cfg)
    assert "MAX_THINKING_TOKENS" not in harness.env_overrides
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
    overrides = {
        "ground-default": NullHarness("Recommended R-1."),
        "horizon-default": NullHarness(lambda req: HarnessResult(ok=True, text="done")),
    }
    orch = build_orchestrator(tmp_path, harnesses=overrides)
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

    # Round 0 finished, so it is skipped; resume re-runs only round 1.
    assert [r.round_index for r in reports] == [1]
    assert reports[0].tasks_run == ("R-1",)
    # The opening ground was not re-run, and round 1's horizon was recreated.
    after = sorted(p.name for p in sessions_dir.iterdir())
    assert after[:5] == ["0001-ground", "0002-system", "0003-horizon-R-1", "0004-system", "0005-ground"]
    assert any("horizon-R-1" in name for name in after[5:])


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
    _queue_task(orch)
    orch.run(RunRecord(id="", focus=Focus(tasks=("R-1",)), rounds_requested=1))
    runlog = orch.run_logs.get("0001")

    recovered = orch._recover_horizon_result(runlog, 0)
    assert recovered is not None
    assert recovered.task_id == "R-1"          # the horizon session, not "" from -system
    assert recovered.status is TaskStatus.FAILED
    assert "## Checklist" not in recovered.report  # i.e. not the system session's report


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
