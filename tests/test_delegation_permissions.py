"""Delegation permissions: default-deny consent record an agent reads."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.config.loader import load_config
from archon_horizon.config.schema import DelegationConfig

_BASE = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
__DELEGATION__
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def _ws(tmp_path: Path, delegation: str = "") -> Path:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_BASE.replace("__DELEGATION__", delegation), "utf-8")
    return ws


def test_default_is_deny() -> None:
    d = DelegationConfig.from_raw({})
    assert d.allow_launch_tasks is False
    assert d.allow_launch_runs is False
    assert d.max_parallel_sessions == 0
    assert d.accounts == ()


def test_free_form_raw_round_trips(tmp_path: Path) -> None:
    delegation = (
        "  delegation:\n"
        "    allow_launch_tasks: true\n"
        "    allow_launch_runs: true\n"
        "    max_parallel_sessions: 3\n"
        "    accounts:\n"
        "      - {harness: claude, config_dir: ~/.claude-work, resets: '17:00 UTC'}\n"
        "    note: prefer the work account after 5pm\n"
    )
    cfg = load_config(_ws(tmp_path, delegation))
    d = cfg.delegation
    assert d.allow_launch_tasks and d.allow_launch_runs
    assert d.max_parallel_sessions == 3
    assert d.accounts[0]["config_dir"] == "~/.claude-work"
    # Free-form keys survive verbatim in raw for the agent to read.
    assert d.raw["note"] == "prefer the work account after 5pm"


def test_permissions_cli_json(tmp_path: Path, capsys) -> None:
    ws = _ws(tmp_path)  # no delegation block => deny
    assert main(["--root", str(ws), "permissions", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["allow_launch_tasks"] is False
    assert payload["allow_launch_runs"] is False
