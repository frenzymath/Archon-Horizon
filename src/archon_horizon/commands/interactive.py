"""Launch a configured harness as an *interactive* TTY session.

Horizon's normal :class:`~archon_horizon.harnesses.base.Harness` seam streams a
machine-readable transcript (headless, ``-p``/``--json``) so the orchestrator can
parse it. But sometimes a human wants to *drive* an agent directly — type
follow-up prompts, steer it, ask questions. For that we build a raw interactive
argv from a role's harness config and hand the terminal straight to the engine
(no ``-p``, no transcript). Shared by ``horizon init``'s post-init advisor,
``horizon discuss``, and ``horizon run <role> --backend interactive``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InteractiveLaunch:
    argv: list[str]
    env: dict[str, str]
    description: str


def _argv_with_prompt(argv: list[str], prompt: str) -> list[str]:
    from archon_horizon.harnesses.command import PROMPT_TOKEN

    if PROMPT_TOKEN in argv:
        return [prompt if token == PROMPT_TOKEN else token for token in argv]
    return [*argv, prompt]


def build_interactive_launch(harness, prompt: str) -> InteractiveLaunch | None:
    """Build an interactive launch from a resolved harness config.

    Returns ``None`` for the ``null`` harness (nothing to launch). Raises
    ``ValueError`` when the engine binary is missing or the kind has no
    interactive form."""
    from archon_horizon.config.harnesses import _env_overrides, _resolve_claude_provider

    env = dict(os.environ)
    description = f"{harness.kind} harness {harness.name!r}"

    if harness.kind == "claude-code":
        model, provider_env = _resolve_claude_provider(harness)
        env.update(provider_env)
        env.update(_env_overrides(harness))
        argv = ["claude"]
        if model:
            argv += ["--model", model]
        argv += list(harness.args)
        if shutil.which(argv[0]) is None:
            raise ValueError("Claude Code is not installed; run `horizon setup` or install `claude`")
        return InteractiveLaunch(_argv_with_prompt(argv, prompt), env, description)

    if harness.kind == "codex":
        env.update(_env_overrides(harness))
        argv = ["codex"]
        if harness.model:
            argv += ["-m", harness.model]
        effort = harness.options.get("effort")
        if effort:
            argv += ["-c", f"model_reasoning_effort={effort}"]
        argv += list(harness.args)
        if shutil.which(argv[0]) is None:
            raise ValueError("Codex is not installed; install `codex` or choose another harness")
        return InteractiveLaunch(_argv_with_prompt(argv, prompt), env, description)

    if harness.kind in {"command", "external-agent"}:
        if not harness.command:
            raise ValueError(f"Harness {harness.name!r} needs a command")
        env.update(_env_overrides(harness))
        return InteractiveLaunch(_argv_with_prompt([harness.command, *harness.args], prompt), env, description)

    if harness.kind == "null":
        return None

    raise ValueError(f"no interactive launcher for harness kind {harness.kind!r}")


def _harness_for_role(cfg, role: str):
    """Resolve the harness config backing ``ground`` or ``horizon``."""
    name = cfg.ground_harness if role == "ground" else cfg.horizon_harness
    if not name:
        raise ValueError(f"config.yaml does not define a {role.capitalize()} harness")
    try:
        return cfg.harnesses[name]
    except KeyError as exc:
        raise ValueError(f"{role.capitalize()} harness {name!r} is not defined") from exc


def interactive_launch_for_role(root: Path, role: str, prompt: str) -> InteractiveLaunch | None:
    """Build an interactive launch from the harness backing ``role``."""
    from archon_horizon.config.loader import load_config

    cfg = load_config(root)
    return build_interactive_launch(_harness_for_role(cfg, role), prompt)


def run_interactive(launch: InteractiveLaunch, cwd: Path) -> int:
    """Hand the terminal to the engine; returns its exit code."""
    return subprocess.run(launch.argv, cwd=cwd, env=launch.env, check=False).returncode


# ── seed prompts ─────────────────────────────────────────────────────────────
#
# Interactive sessions are *human-driven*, so we don't compose the full
# orchestrated agent prompt (which assumes a specific task/round). Instead we seed
# a short role brief that points the engine at the workspace, the docs, and the
# on-disk state, then let the human steer from there.

_ORIENTATION = """\
You are running inside an **Archon Horizon** workspace at `{root}`.

Archon Horizon orchestrates AI agents that formalize mathematics in Lean 4 across
multiple projects. To understand the system, read (in the *package install*, not
necessarily this workspace):

- `README.md` — the detailed, self-contained reference for the whole tool.
- `docs/` — deeper guides per topic (architecture, workspaces, orchestration,
  inboxes, blueprints/leandag, dashboard/search, CLI reference).

This workspace's live state lives under `.archon-horizon/` — `runs/` (session
transcripts and reports), `tasks/`, `inbox/`, `blueprints/`, `roadmap`,
`memory.md` — and its manifest is `config.yaml`. Read those to see the current
status and what recent runs did. Prefer the `horizon` CLI for changes."""

_ROLE_BRIEF = {
    "ground": """\
Act as the **Ground agent**: the human-aligned strategist. You maintain the
blueprints, dependency DAGs, roadmap, tasks, memory, and inboxes — and supervise
the Long Horizon prover. You do not run long Lean proof searches yourself.""",
    "horizon": """\
Act as the **Long Horizon agent**: the autonomous prover. You turn blueprint
nodes into checked Lean — read the blueprint node first, build with `lake`,
diagnose compiler errors, and repair proofs. Report what you proved and what
remains.""",
}

_DISCUSS_BRIEF = """\
You are the **discuss** agent: a human-facing companion, essentially the Ground
agent but here purely to talk with the human and do what they ask. Your job:

- Explain the current status of this workspace and what recent runs did (read the
  run transcripts/reports under `.archon-horizon/runs/`).
- Answer questions about Archon Horizon itself — read `README.md`/`docs/` and,
  when a detail isn't documented, the package source.
- Help manage the workspace: add or adjust projects, tasks, inbox items, roadmap
  entries, blueprints — using the `horizon` CLI.

Rules of engagement:
- Only *modify* anything when the human explicitly asks you to. Otherwise
  explain, propose, and wait.
- Be concrete: cite exact files, task ids, and commands.
- Start by briefly greeting the human and offering a short status summary, then
  ask what they'd like to do."""


def interactive_role_prompt(root: Path, role: str) -> str:
    """Seed prompt for `horizon run <role> --backend interactive`."""
    brief = _ROLE_BRIEF.get(role, _ROLE_BRIEF["ground"])
    return f"{_ORIENTATION.format(root=root)}\n\n{brief}\n"


def discuss_prompt(root: Path) -> str:
    """Seed prompt for the `horizon discuss` companion agent."""
    return f"{_ORIENTATION.format(root=root)}\n\n{_DISCUSS_BRIEF}\n"
