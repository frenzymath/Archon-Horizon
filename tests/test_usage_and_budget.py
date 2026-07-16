"""Phase E: consumption awareness and limit robustness.

- The harness streams a live per-session ``usage.json`` any engine's agent can
  poll via ``horizon usage``.
- Failure classification reads the engine's typed error events, not just stderr.
- An advertised "retry after Xs" stretches the backoff and is surfaced.
- workspace.budget stops runs cleanly (pause marker + resumable state).
"""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.config.schema import BudgetConfig
from archon_horizon.harnesses.command import _UsageFile, _advertised_retry_s, _classify_failure
from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind, TranscriptUsage
from archon_horizon.transcript.parsers import parse_claude_line


def test_usage_file_accumulates_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "usage.json"
    uf = _UsageFile(path)
    uf.add(TranscriptEvent(TranscriptKind.USAGE, usage=TranscriptUsage(tokens_in=10, tokens_out=100, cost_usd=0.5)))
    uf.add(TranscriptEvent(TranscriptKind.USAGE, usage=TranscriptUsage(tokens_in=5, tokens_out=50)))
    uf.add(TranscriptEvent(TranscriptKind.TEXT, text="not usage"))  # ignored
    uf.flush(force=True)

    data = json.loads(path.read_text("utf-8"))
    assert data["tokens_in"] == 15
    assert data["tokens_out"] == 150
    assert data["cost_usd"] == 0.5
    assert data["usage_events"] == 2
    assert data["updated_at"] is not None


def test_usage_file_without_path_is_noop() -> None:
    uf = _UsageFile(None)
    uf.add(TranscriptEvent(TranscriptKind.USAGE, usage=TranscriptUsage(tokens_out=1)))
    uf.flush(force=True)  # must not raise


def test_claude_error_result_yields_typed_error_event() -> None:
    line = json.dumps({
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "Rate limit reached; try again in 42 seconds",
    })
    events = parse_claude_line(line)
    errors = [e for e in events if e.kind is TranscriptKind.ERROR]
    assert len(errors) == 1
    assert errors[0].data.get("subtype") == "error_during_execution"
    # Classification reads the typed event's text.
    assert _classify_failure(errors[0].text) == "rate_limit"


def test_advertised_retry_window_is_parsed_and_capped() -> None:
    assert _advertised_retry_s("Rate limit; retry after 30s please") == 30.0
    assert _advertised_retry_s("try again in 5 seconds") == 5.0
    assert _advertised_retry_s("Retry-After: 12") == 12.0
    assert _advertised_retry_s("retry after 999999s") == 15 * 60.0  # capped
    assert _advertised_retry_s("no hint here") is None


def test_budget_config_parses_and_reports_configured() -> None:
    empty = BudgetConfig.from_raw({})
    assert not empty.configured
    cfg = BudgetConfig.from_raw({"session_tokens_out": "1000", "run_cost_usd": 2.5})
    assert cfg.configured
    assert cfg.session_tokens_out == 1000
    assert cfg.run_cost_usd == 2.5
    assert cfg.run_tokens_out is None


def _orchestrator(tmp_path: Path, harness, budget: BudgetConfig | None):
    from archon_horizon.config.loader import build_orchestrator
    from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet

    ws = tmp_path / "ws"
    (ws / "projects" / "p").mkdir(parents=True)
    (ws / "config.yaml").write_text(
        "workspace:\n"
        "  name: w\n"
        "  rounds: 3\n"
        "  horizon_agent: {harness: hor}\n"
        "harnesses:\n"
        "  hor: {kind: \"null\"}\n"
        "projects:\n"
        "  p: {path: projects/p}\n",
        "utf-8",
    )
    orch = build_orchestrator(ws, harnesses={"hor": harness})
    orch.budget = budget
    orch.task_store.put(HorizonTask(
        id="T-1", project="p", objective="x", title="x",
        projects=("p",), status=TaskStatus.QUEUED, write_set=WriteSet(projects=("p",)),
    ))
    return orch


def test_run_budget_stops_cleanly_with_pause_marker(tmp_path: Path) -> None:
    from archon_horizon.core.sessions import Focus, RunRecord
    from archon_horizon.harnesses.base import HarnessRequest, HarnessResult, Usage
    from archon_horizon.harnesses.null import NullHarness

    calls: list[str] = []

    def burn(req: HarnessRequest) -> HarnessResult:
        calls.append(req.prompt)
        return HarnessResult(ok=True, text="did work", usage=Usage(tokens_in=1, tokens_out=500))

    orch = _orchestrator(tmp_path, NullHarness(burn), BudgetConfig(run_tokens_out=100))
    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=3))

    # First session crossed the 100-token run budget: no further sessions ran.
    assert len(calls) == 1
    assert orch.last_paused is not None
    assert orch.last_paused["reason"] == "budget"
    run_id = str(orch.last_paused["run_id"])
    marker = json.loads(
        (orch.workspace.state_path / "runs" / run_id / "paused.json").read_text("utf-8")
    )
    assert marker["reason"] == "budget"
    assert marker["focus_tasks"] == ["T-1"]
    stopped = [e for e in orch.event_log.read_all() if e.type == "run.stopped"]
    assert stopped and stopped[-1].data["reason"] == "budget"


def test_usage_limit_writes_pause_marker_with_retry_hint(tmp_path: Path) -> None:
    from archon_horizon.core.sessions import Focus, RunRecord
    from archon_horizon.harnesses.base import HarnessRequest, HarnessResult
    from archon_horizon.harnesses.null import NullHarness

    def limited(req: HarnessRequest) -> HarnessResult:
        return HarnessResult(
            ok=False, text="usage limit reached",
            metadata={"failure_reason": "usage_limit", "retry_after_s": 120.0},
        )

    orch = _orchestrator(tmp_path, NullHarness(limited), None)
    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=3))

    assert orch.last_paused is not None
    assert orch.last_paused["reason"] == "usage_limit"
    assert orch.last_paused["retry_after_s"] == 120.0


def test_new_run_clears_stale_pause_marker(tmp_path: Path) -> None:
    from archon_horizon.core.sessions import Focus, RunRecord
    from archon_horizon.harnesses.base import HarnessRequest, HarnessResult
    from archon_horizon.harnesses.null import NullHarness

    def ok(req: HarnessRequest) -> HarnessResult:
        return HarnessResult(ok=True, text="fine")

    orch = _orchestrator(tmp_path, NullHarness(ok), None)
    orch.run(RunRecord(id="", focus=Focus(tasks=("T-1",)), rounds_requested=1))
    assert orch.last_paused is None


def test_usage_cli_reads_session_and_run(tmp_path: Path, capsys, monkeypatch) -> None:
    from archon_horizon.cli import main

    ws = tmp_path / "ws"
    (ws / "projects" / "p").mkdir(parents=True)
    (ws / "config.yaml").write_text(
        "workspace:\n"
        "  name: w\n"
        "  horizon_agent: {harness: hor}\n"
        "  budget: {run_tokens_out: 1000}\n"
        "harnesses:\n"
        "  hor: {kind: \"null\"}\n"
        "projects: {}\n",
        "utf-8",
    )
    session = ws / ".archon-horizon" / "runs" / "0001" / "sessions" / "0001-horizon-T-1"
    session.mkdir(parents=True)
    (session / "usage.json").write_text(json.dumps({
        "tokens_in": 10, "tokens_out": 600, "cached_tokens_in": 0,
        "reasoning_tokens_out": 0, "cost_usd": 1.25, "usage_events": 3,
    }), "utf-8")

    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0001")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "0001-horizon-T-1")
    assert main(["--root", str(ws), "usage", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["run"] == "0001"
    assert payload["session_usage"]["tokens_out"] == 600
    assert payload["run_usage"]["tokens_out"] == 600
    assert payload["budget"]["run_tokens_out"] == 1000
    assert payload["headroom"]["run_tokens_out"] == 400
