"""``horizon discuss`` — an interactive, human-facing workspace advisor.

Unlike ``horizon run`` (which drives headless Horizon sessions), ``discuss``
hands the terminal straight to the engine and seeds it with a brief: it knows what
Archon Horizon is (it reads the README/docs and, when needed, the source),
explains the current status and what recent runs did, and can manage the
workspace (projects, tasks, inbox, roadmap) — but only *modifies* things when
you explicitly ask.
"""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log

from .interactive import discuss_prompt, interactive_launch_for_role, run_interactive


def discuss(ctx: typer.Context) -> None:
    """Open the human-approved workspace coordination console."""
    root: Path = ctx.obj["root"]
    log.header("Discuss")
    prompt = discuss_prompt(root.resolve())
    try:
        # discuss is a human-driven advisor: launch WITHOUT the Horizon inbox
        # attention hooks, which inject per-tool inbox context meant for
        # autonomous runs and would interrupt a back-and-forth conversation.
        launch = interactive_launch_for_role(root, "horizon", prompt, attention_hooks=False)
    except Exception as exc:
        log.error(f"Could not launch the discuss agent: {exc}")
        raise typer.Exit(1)
    if launch is None:
        log.error("The Horizon harness is 'null'; there is no engine to talk to.")
        raise typer.Exit(1)
    log.info(f"Launching the discuss agent using {launch.description}.")
    run_interactive(launch, root)
