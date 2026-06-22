"""Structured console output for the Archon Horizon CLI.

This follows Archon's pattern: command modules do not print directly. They
route human-facing output through one logger so formatting and stdout/stderr
discipline can evolve in one place.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

_console = Console()
_err_console = Console(stderr=True)
_PREFIX = "[bold cyan]\\[HORIZON][/bold cyan]"
_REPO_URL = "https://github.com/frenzymath/Archon-Horizon"
_BANNER_TEXT = "Archon Horizon"
_BANNER_FONT = "smslant"


def _banner_text() -> str:
    """Render the banner with pyfiglet when available.

    The dependency is declared in ``pyproject.toml``. The fallback keeps
    source-tree invocations working before an editable install refreshes the
    environment.
    """
    try:
        from pyfiglet import figlet_format
    except ImportError:
        return _BANNER_TEXT
    return figlet_format(_BANNER_TEXT, font=_BANNER_FONT).rstrip()


class HorizonLog:
    """Small Rich-backed logger used by CLI commands."""

    def banner(self, version: str) -> None:
        _err_console.print(f"\n[bold cyan]{_banner_text()}[/bold cyan]\n")
        meta = Text.assemble((f" v{version} ", "bold cyan"), "  ", (f" {_REPO_URL} ", "dim italic"))
        _err_console.print(Rule(title=meta, style="cyan", align="left"))
        _err_console.print()

    def info(self, msg: str) -> None:
        _console.print(f"{_PREFIX} {msg}")

    def success(self, msg: str) -> None:
        _console.print(f"{_PREFIX} [green]✓ {msg}[/green]")

    def warn(self, msg: str) -> None:
        _console.print(f"{_PREFIX} [yellow]⚠ {msg}[/yellow]")

    def error(self, msg: str) -> None:
        _console.print(f"{_PREFIX} [bold red]✗ {msg}[/bold red]")

    def step(self, msg: str) -> None:
        _console.print(f"{_PREFIX} [dim]›[/dim] {msg}")

    def header(self, title: str) -> None:
        _console.print()
        _console.print(Rule(title=f"[bold]{title}[/bold]", style="cyan", align="left"))

    def key_value(self, data: dict[str, str], title: str = "") -> None:
        table = Table(show_header=False, border_style="dim", padding=(0, 2), title=title or None)
        table.add_column("Key", style="bold", no_wrap=True)
        table.add_column("Value")
        for key, value in data.items():
            table.add_row(key, value)
        _console.print(table)

    def results_table(self, rows: list[tuple[str, str, str]], title: str = "") -> None:
        table = Table(border_style="dim", padding=(0, 1), title=title or None)
        table.add_column("Name", style="bold", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Detail")
        styles = {
            "ok": "green",
            "done": "green",
            "success": "green",
            "error": "red",
            "failed": "red",
            "blocked": "yellow",
            "pending": "dim",
            "skipped": "dim",
        }
        for name, status, detail in rows:
            style = styles.get(status.lower(), "")
            styled = f"[{style}]{status}[/{style}]" if style else status
            table.add_row(name, styled, detail)
        _console.print(table)

    def panel(self, content: str, title: str = "", style: str = "cyan") -> None:
        _console.print(Panel(content, title=title or None, border_style=style, padding=(1, 2)))


log = HorizonLog()
