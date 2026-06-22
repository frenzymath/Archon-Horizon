"""Archon Horizon CLI entrypoint.

This mirrors Archon's structure: the top-level file owns the Typer app and
command registration only. Each command module owns its CLI options and
delegates behavior to small command classes or library services.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import click
import typer
import typer.core
from typer.main import get_command

from archon_horizon import __version__
from archon_horizon.commands import blueprint as blueprint_cmd
from archon_horizon.commands import dashboard as dashboard_cmd
from archon_horizon.commands import inbox as inbox_cmd
from archon_horizon.commands import init as init_cmd
from archon_horizon.commands import project as project_cmd
from archon_horizon.commands import roadmap as roadmap_cmd
from archon_horizon.commands import run as run_cmd
from archon_horizon.commands import subagent as subagent_cmd
from archon_horizon.commands import setup as setup_cmd
from archon_horizon.commands import sync as sync_cmd
from archon_horizon.log import log


class _BannerGroup(typer.core.TyperGroup):
    """Typer group that prints the Horizon banner before help output."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        log.banner(__version__)
        super().format_help(ctx, formatter)


app = typer.Typer(
    cls=_BannerGroup,
    help="Workspace-first orchestration for long-horizon Lean formalization agents.",
    invoke_without_command=True,
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        log.banner(__version__)
        raise typer.Exit()


@app.callback()
def callback(
    ctx: typer.Context,
    root: Path = typer.Option(Path("."), "--root", help="Workspace root."),
    version: bool | None = typer.Option(
        None,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Workspace-first orchestration for long-horizon Lean formalization agents."""
    ctx.obj = {"root": root}
    if ctx.invoked_subcommand is None and not version:
        log.banner(__version__)


# ── register commands ────────────────────────────────────────────────

app.command()(init_cmd.init)
app.command("setup")(setup_cmd.setup)
app.command("run")(run_cmd.run)
app.add_typer(inbox_cmd.app, name="inbox")
app.add_typer(project_cmd.app, name="project")
app.add_typer(roadmap_cmd.app, name="roadmap")
app.command("blueprint")(blueprint_cmd.blueprint)
app.command("sync")(sync_cmd.sync)
app.command("dashboard")(dashboard_cmd.dashboard)
app.command("serve")(dashboard_cmd.serve)
app.command("subagent")(subagent_cmd.subagent)


def main(argv: Sequence[str] | None = None) -> int:
    """Compatibility shim for tests and the console script entry point."""
    cmd = get_command(app)
    try:
        result = cmd.main(
            args=list(argv) if argv is not None else None,
            prog_name="horizon",
            standalone_mode=False,
        )
    except typer.Exit as exc:
        return int(exc.exit_code or 0)
    except click.exceptions.Exit as exc:
        return int(exc.exit_code or 0)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code or 1)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
