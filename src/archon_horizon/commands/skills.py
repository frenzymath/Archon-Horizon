"""Typer command group for the bundled skills.

Skills are copied into a workspace's ``.claude/skills/`` at ``init`` time, so a
workspace keeps its own copy that an upgraded Horizon does not touch
automatically. ``horizon skills install`` refreshes them to the bundled
versions — run it after upgrading so the agents read the latest guidance (e.g.
the Markdown / commenting conventions).
"""

from __future__ import annotations

from pathlib import Path

import typer

from archon_horizon.log import log
from archon_horizon.skills.registry import available_skills, install_skills

from .shared import emit_json

app = typer.Typer(help="Install or refresh the bundled skills under .claude/skills/.", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


@app.command("list")
def list_skills(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """List the skills bundled with this Horizon."""
    skills = available_skills()
    if as_json:
        emit_json(
            {
                "skills": [
                    {
                        "name": s.name,
                        "description": s.description,
                        **({"recommendation": s.recommendation} if s.recommendation else {}),
                    }
                    for s in skills
                ]
            }
        )
        return
    for skill in skills:
        log.info(f"{skill.name} — {skill.description}")
        if skill.recommendation:
            log.info(f"  advisory recommendation: {skill.recommendation}")


@app.command("install")
def install(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """Install or refresh the bundled skills into this workspace's .claude/skills/.

    Skills that differ from the bundled version are overwritten (replacing any
    local edits); unchanged ones are left as-is. Run this after upgrading Horizon
    so the agents pick up the latest skill text.
    """
    root = Path(ctx.obj["root"])
    installed = install_skills(root)  # overwrite=None → refresh anything that differs
    if as_json:
        emit_json({"installed": installed})
        return
    if installed:
        log.success(f"Installed/updated {len(installed)} skill(s): {', '.join(installed)}.")
    else:
        log.info("All skills are already up to date.")
