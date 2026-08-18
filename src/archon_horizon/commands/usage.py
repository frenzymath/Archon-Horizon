"""``horizon usage`` — the agent-facing consumption gauge.

Engine-agnostic: the harness streams every session's token usage into a live
``usage.json`` next to its transcript, and this command reads those files back
— so an agent (Claude Code, Codex, anything that can shell out) can check its
own burn rate mid-session before fanning out subagents or starting heavy work.

Resolution mirrors the rest of the CLI: inside a session the ``ARCHON_HORIZON_
RUN``/``ARCHON_HORIZON_SESSION`` env identifies "me"; outside one, the latest
run is shown.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import typer

from archon_horizon.log import log

from .shared import emit_json

_USAGE_KEYS = ("tokens_in", "tokens_out", "cached_tokens_in", "reasoning_tokens_out")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _session_usage(session_dir: Path) -> dict[str, Any]:
    """A session's usage, repairing legacy completed totals from its transcript."""
    live = _read_json(session_dir / "usage.json")
    if int(live.get("schema_version") or 0) >= 2 and (
        live.get("usage_events") or live.get("compaction_count") or live.get("context")
    ):
        return live
    meta = _read_json(session_dir / "meta.json")
    transcript_path = session_dir / "transcript.jsonl"
    # Version-1 files summed repeated display usage and cumulative Claude costs.
    # Completed session transcripts are immutable, so repair them on read. Live
    # legacy sessions keep their cheap usage.json snapshot until they finish.
    if transcript_path.exists() and meta.get("ended_at"):
        from archon_horizon.transcript.parsers import aggregate_usage
        from archon_horizon.transcript.sink import read_transcript

        events = read_transcript(transcript_path)
        usage = aggregate_usage(events)
        return {
            "schema_version": 2,
            "tokens_in": usage.tokens_in,
            "tokens_out": usage.tokens_out,
            "cached_tokens_in": usage.cached_tokens_in,
            "reasoning_tokens_out": usage.reasoning_tokens_out,
            "cost_usd": usage.cost_usd,
            "usage_events": sum(event.kind == "usage" for event in events),
        }
    if live.get("usage_events"):
        return live
    meta_usage = meta.get("usage")
    return meta_usage if isinstance(meta_usage, dict) else {}


def _sum_sessions(run_dir: Path) -> tuple[dict[str, Any], list[str]]:
    totals: dict[str, Any] = {key: 0 for key in _USAGE_KEYS}
    totals["cost_usd"] = None
    failure_reasons: list[str] = []
    sessions_dir = run_dir / "sessions"
    if not sessions_dir.is_dir():
        return totals, failure_reasons
    for session_dir in sorted(p for p in sessions_dir.iterdir() if p.is_dir()):
        usage = _session_usage(session_dir)
        for key in _USAGE_KEYS:
            totals[key] += int(usage.get(key) or 0)
        cost = usage.get("cost_usd")
        if cost is not None:
            totals["cost_usd"] = (totals["cost_usd"] or 0.0) + float(cost)
        reason = _read_json(session_dir / "meta.json").get("failure_reason")
        if reason:
            failure_reasons.append(str(reason))
    return totals, failure_reasons


def _headroom(limit: int | float | None, used: int | float | None) -> Any:
    if limit is None:
        return None
    return max(0, limit - (used or 0))


def usage(
    ctx: typer.Context,
    run_id: str | None = typer.Option(None, "--run", help="Run id to inspect (default: the current session's run, else the latest)."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Show token/cost consumption: this session, this run, budget headroom, and
    recent rate/usage-limit signals. Cheap to call mid-session."""
    from archon_horizon.config.loader import load_config

    root: Path = ctx.obj["root"]
    cfg = load_config(root)
    runs_dir = root / cfg.state_dir / "runs"

    resolved_run = (run_id or os.environ.get("ARCHON_HORIZON_RUN") or "").strip()
    if not resolved_run:
        candidates = sorted(p.name for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
        resolved_run = candidates[-1] if candidates else ""
    if not resolved_run:
        log.error("No runs recorded in this workspace yet.")
        raise typer.Exit(1)
    run_dir = runs_dir / resolved_run

    session_name = os.environ.get("ARCHON_HORIZON_SESSION", "").strip()
    session = _session_usage(run_dir / "sessions" / session_name) if session_name else {}
    run_totals, failure_reasons = _sum_sessions(run_dir)
    paused = _read_json(run_dir / "paused.json")

    budget = cfg.budget
    budgets = {
        "session_tokens_out": budget.session_tokens_out,
        "run_tokens_out": budget.run_tokens_out,
        "run_cost_usd": budget.run_cost_usd,
    }
    headroom = {
        "session_tokens_out": _headroom(budget.session_tokens_out, session.get("tokens_out")),
        "run_tokens_out": _headroom(budget.run_tokens_out, run_totals.get("tokens_out")),
        "run_cost_usd": _headroom(budget.run_cost_usd, run_totals.get("cost_usd")),
    }
    limit_signals = [r for r in failure_reasons if r in ("rate_limit", "usage_limit", "overloaded")]

    if as_json:
        emit_json({
            "run": resolved_run,
            "session": session_name or None,
            "session_usage": session or None,
            "run_usage": run_totals,
            "budget": budgets,
            "headroom": headroom,
            "recent_failure_reasons": failure_reasons,
            "paused": paused or None,
        })
        return

    rows: list[tuple[str, str, str]] = []

    def _fmt(usage_dict: dict[str, Any]) -> str:
        if not usage_dict:
            return "-"
        cost = usage_dict.get("cost_usd")
        cost_s = f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""
        return (
            f"in {usage_dict.get('tokens_in') or 0:,} / out {usage_dict.get('tokens_out') or 0:,}"
            f"{cost_s}"
        )

    if session_name:
        rows.append((f"session {session_name}", "usage", _fmt(session)))
    rows.append((f"run {resolved_run}", "usage", _fmt(run_totals)))
    for key, limit in budgets.items():
        if limit is not None:
            rows.append((key, "budget", f"{limit:,} (headroom {headroom[key]:,})"
                         if isinstance(limit, int) else f"${limit} (headroom ${headroom[key]:.2f})"))
    if limit_signals:
        rows.append(("recent limits", "signal", ", ".join(limit_signals)))
    if paused:
        rows.append(("paused", paused.get("reason", ""), paused.get("resume", "")))
    log.results_table(rows, title="Usage")
