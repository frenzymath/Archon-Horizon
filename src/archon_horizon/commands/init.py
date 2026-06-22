"""Typer-decorated ``init`` entry point."""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log

_CONFIG_TEMPLATE = """\
workspace:
  name: {name}
  state_dir: .archon-horizon
  rounds: 5
  informal_agent:
    harness: informal-default
  horizon_agent:
    harness: horizon-default
  scheduler:
    max_parallel_sessions: 1

harnesses:
  informal-default:
    kind: {informal_kind}
    model: sonnet
  horizon-default:
    kind: {horizon_kind}
    model: fable5
    options:
      effort: high

github:
  enabled: false
  repo: owner/repo

projects: {{}}
"""

_SUBAGENT_WRAPPER = """\
#!/usr/bin/env python3
\"\"\"Archon Horizon subagent wrapper.

Agents call this from Bash so child subagents run synchronously and stream
their own transcript through the normal Archon Horizon harness path.
\"\"\"

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--name", required=True)
    known, rest = parser.parse_known_args()
    cmd = [
        sys.executable,
        "-m",
        "archon_horizon",
        "--root",
        str(Path.cwd()),
        "subagent",
        known.name,
        *rest,
    ]
    return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())
"""


class InitCommand:
    def __init__(
        self, 
        root: Path, 
        *, 
        name: str | None = None, 
        informal_harness: str | None = None,
        horizon_harness: str | None = None,
        goal: str | None = None,
        interactive: bool = True
    ) -> None:
        self.root = root
        self.name = name
        self.informal_harness = informal_harness
        self.horizon_harness = horizon_harness
        self.goal = goal
        self.interactive = interactive

    def run(self) -> None:
        config_path = self.root / "config.yaml"
        if config_path.exists():
            log.error(f"config.yaml already exists at {config_path}")
            raise typer.Exit(1)
        
        name = self.name or self.root.resolve().name
        informal_kind = self.informal_harness or "claude-code"
        horizon_kind = self.horizon_harness or "codex"
        goal = self.goal or ""

        if self.interactive:
            from rich.prompt import Prompt
            if not self.name:
                name = Prompt.ask("Workspace name", default=name)
            if not self.informal_harness:
                informal_kind = Prompt.ask(
                    "Informal agent harness", 
                    choices=["claude-code", "antigravity", "codex", "command"], 
                    default=informal_kind
                )
            if not self.horizon_harness:
                horizon_kind = Prompt.ask(
                    "Horizon agent harness", 
                    choices=["claude-code", "antigravity", "codex", "command"], 
                    default=horizon_kind
                )
            if not self.goal:
                goal = Prompt.ask("Initial project goal (optional)")

        self.root.mkdir(parents=True, exist_ok=True)
        config_text = _CONFIG_TEMPLATE.format(
            name=name,
            informal_kind=informal_kind,
            horizon_kind=horizon_kind,
        )
        config_path.write_text(config_text, "utf-8")
        
        for sub in ("inboxes", "inboxes/local", "tasks", "proposals", "runs", "reports", "artifacts", "locks", "vcs", "subagents"):
            (self.root / ".archon-horizon" / sub).mkdir(parents=True, exist_ok=True)

        if goal:
            import json
            import uuid
            draft = {"kind": "feature", "body": f"Initial goal: {goal}\n\nPlease break this down and plan the work.", "labels": ["pending"]}
            goal_path = self.root / ".archon-horizon" / "inboxes" / "local" / f"{uuid.uuid4().hex}.json"
            goal_path.write_text(json.dumps(draft, indent=2), "utf-8")

        tools = self.root / ".claude" / "tools"
        tools.mkdir(parents=True, exist_ok=True)
        wrapper = tools / "horizon-subagent.py"
        wrapper.write_text(_SUBAGENT_WRAPPER, "utf-8")
        wrapper.chmod(0o755)
        log.success(f"Initialized workspace at {self.root}")


def init(
    ctx: typer.Context,
    name: str | None = typer.Option(None, "--name", help="Workspace name (default: directory name)."),
    informal_harness: str | None = typer.Option(None, "--informal-harness", help="Harness for the informal agent."),
    horizon_harness: str | None = typer.Option(None, "--horizon-harness", help="Harness for the horizon agent."),
    goal: str | None = typer.Option(None, "--goal", help="Initial project goal to seed the inbox."),
    interactive: bool = typer.Option(True, help="Run interactively. Pass --no-interactive to accept defaults without prompting."),
) -> None:
    """Scaffold a new workspace."""
    InitCommand(
        ctx.obj["root"], 
        name=name, 
        informal_harness=informal_harness,
        horizon_harness=horizon_harness,
        goal=goal,
        interactive=interactive
    ).run()
