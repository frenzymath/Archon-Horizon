"""``horizon discuss`` — an interactive, human-facing companion agent.

Unlike ``horizon run`` (which drives headless, orchestrated ground/horizon
sessions), ``discuss`` hands the terminal straight to the engine and seeds it
with a brief that makes it a Ground-flavored companion: it already knows what
Archon Horizon is (it reads the README/docs and, when needed, the source),
explains the current status and what recent runs did, and can manage the
workspace (projects, tasks, inbox, roadmap) — but only *modifies* things when
you explicitly ask. Think of it as sitting down to talk with the Ground agent.
"""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log

from .interactive import discuss_prompt, interactive_launch_for_role, run_interactive


def discuss(ctx: typer.Context) -> None:
    """Open an interactive session to talk with the workspace (status, changes)."""
    root: Path = ctx.obj["root"]
    log.header("Discuss")
    prompt = discuss_prompt(root.resolve())
    try:
        # The companion uses the Ground harness (the human-aligned engine).
        launch = interactive_launch_for_role(root, "ground", prompt)
    except Exception as exc:
        log.error(f"Could not launch the discuss agent: {exc}")
        raise typer.Exit(1)
    if launch is None:
        log.error("The Ground harness is 'null'; there is no engine to talk to.")
        raise typer.Exit(1)
    log.info(f"Launching the discuss agent using {launch.description}.")
    run_interactive(launch, root)
