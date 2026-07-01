"""Typer-decorated ``update`` entry point.

Self-update Horizon by re-running the canonical remote install script, then
nudge the user to refresh their existing workspaces with ``horizon init
--update`` (managed files such as skills/subagents/MCP are version-coupled).
"""

from __future__ import annotations

import subprocess

import typer

from archon_horizon.log import log

from .shared import emit_json

# Single source of truth for the install/update endpoint; keep in sync with
# install.sh and the README. Migrating GitHub orgs is a one-line change here.
INSTALL_URL = "https://raw.githubusercontent.com/frenzymath/Archon-Horizon/refs/heads/main/install.sh"


def update(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Update Archon Horizon to the latest version by re-running the install script."""
    log.step("Fetching and running the Archon Horizon install script...")
    result = subprocess.run(
        ["bash", "-c", f"curl -sSL {INSTALL_URL} | bash"],
        text=True,
    )
    ok = result.returncode == 0
    if as_json:
        emit_json({"updated": ok, "returncode": result.returncode, "source": INSTALL_URL})
        if not ok:
            raise typer.Exit(result.returncode)
        return

    if not ok:
        log.error("Update failed — the install script exited with an error.")
        raise typer.Exit(result.returncode)
    log.success("Archon Horizon updated successfully.")
    log.warn(
        "If you have workspaces created with an older Horizon, run `horizon init --update` "
        "in each to refresh managed files (skills, subagents, MCP). Back up your work first."
    )
