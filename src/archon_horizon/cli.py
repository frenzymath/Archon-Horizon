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
from archon_horizon.commands import commit as commit_cmd
from archon_horizon.commands import dashboard as dashboard_cmd
from archon_horizon.commands import discuss as discuss_cmd
from archon_horizon.commands import inbox as inbox_cmd
from archon_horizon.commands import init as init_cmd
from archon_horizon.commands import leandag as leandag_cmd
from archon_horizon.commands import project as project_cmd
from archon_horizon.commands import roadmap as roadmap_cmd
from archon_horizon.commands import run as run_cmd
from archon_horizon.commands import search as search_cmd
from archon_horizon.commands import task as task_cmd
from archon_horizon.commands import subagent as subagent_cmd
from archon_horizon.commands import setup as setup_cmd
from archon_horizon.commands import skills as skills_cmd
from archon_horizon.commands import sync as sync_cmd
from archon_horizon.commands import update as update_cmd
from archon_horizon.log import log


class _BannerGroup(typer.core.TyperGroup):
    """Typer group that prints the Horizon banner before help output."""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        log.banner(__version__)
        super().format_help(ctx, formatter)


import typer.rich_utils

# Override Typer's default Rich colors with our hex palette
typer.rich_utils.STYLE_OPTIONS_PANEL_BORDER = "#d8b4fe"
typer.rich_utils.STYLE_COMMANDS_PANEL_BORDER = "#d8b4fe"
typer.rich_utils.STYLE_USAGE = "bold #d8b4fe"
typer.rich_utils.STYLE_OPTION = "bold #fde047"
typer.rich_utils.STYLE_SWITCH = "bold #86efac"
typer.rich_utils.STYLE_COMMANDS_TABLE_FIRST_COLUMN = "bold #93c5fd"
typer.rich_utils.STYLE_METAVAR = "bold #f9a8d4"
typer.rich_utils.STYLE_HELPTEXT_FIRST_LINE = ""

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


def _machine_output_requested() -> bool:
    """True when the invocation asks for machine-readable output (``--json``).

    The callback runs before the subcommand parses its own options, so we peek
    at the raw args. When set, we suppress the banner and any human chrome so
    stdout stays clean JSON.
    """
    import sys

    return "--json" in sys.argv[1:]


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
    import os

    # An agent's shell cwd is often a project subdirectory, so a bare
    # `horizon <cmd>` (root defaulting to `.`) wouldn't find the workspace.
    # When the orchestrator stamps ARCHON_HORIZON_ROOT and the caller did not
    # pass --root explicitly, resolve to that workspace root.
    if root == Path(".") and os.environ.get("ARCHON_HORIZON_ROOT"):
        root = Path(os.environ["ARCHON_HORIZON_ROOT"])
    json_mode = _machine_output_requested()
    ctx.obj = {"root": root, "json": json_mode}
    if json_mode:
        # Keep stdout pure JSON: send all human chrome (and the banner) to stderr.
        log.route_to_stderr()
    elif not version:
        log.banner(__version__)


# ── register commands ────────────────────────────────────────────────

app.command()(init_cmd.init)
app.command("setup")(setup_cmd.setup)
app.command("update")(update_cmd.update)
app.command("run")(run_cmd.run)
app.command("commit")(commit_cmd.commit)
app.command("discuss")(discuss_cmd.discuss)
app.add_typer(inbox_cmd.app, name="inbox")
app.add_typer(roadmap_cmd.app, name="roadmap")
app.add_typer(task_cmd.app, name="task")
app.add_typer(project_cmd.app, name="project")
app.add_typer(skills_cmd.app, name="skills")
app.command("blueprint")(blueprint_cmd.blueprint)
app.command("leandag")(leandag_cmd.leandag)
app.command("search")(search_cmd.search)
app.command("sync")(sync_cmd.sync)
app.command("dashboard")(dashboard_cmd.dashboard)
app.command("subagent", hidden=True)(subagent_cmd.subagent)


def main(argv: Sequence[str] | None = None) -> int:
    """Compatibility shim for tests and the console script entry point."""
    import sys

    cmd = get_command(app)
    # Mirror the passed args into sys.argv so option-peeking (e.g. --json banner
    # suppression in the callback) behaves the same in-process as on the CLI.
    saved_argv = sys.argv
    if argv is not None:
        sys.argv = ["horizon", *argv]
    # The callback may route log output to stderr (--json). Bound that mutation
    # to this invocation so it can't leak into a long-lived process or tests.
    saved_route = log._out
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
    except FileNotFoundError as exc:
        # Almost always "not a workspace / missing config" — surface the
        # actionable message cleanly instead of dumping a traceback.
        log.error(str(exc))
        return 1
    except Exception as exc:
        # Typer vendors its own copy of click (``typer._click``), so its
        # ``Exit``/``ClickException`` instances are NOT the ``click`` ones caught
        # above. Catch those duck-typed: a click-style exception exposes an
        # ``exit_code`` (and usually ``show``); anything else is a real bug.
        exit_code = getattr(exc, "exit_code", None)
        if exit_code is None:
            raise
        show = getattr(exc, "show", None)
        if callable(show):
            show()
        return int(exit_code or 1)
    finally:
        sys.argv = saved_argv
        log._out = saved_route
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
