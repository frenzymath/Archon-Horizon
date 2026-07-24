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
_PREFIX = "[bold #d8b4fe]\\[HORIZON][/bold #d8b4fe]"
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

    def __init__(self) -> None:
        # Human-facing chrome goes here. ``route_to_stderr`` flips it to the
        # stderr console so ``--json`` commands keep stdout as pure data.
        self._out = _console

    def route_to_stderr(self) -> None:
        """Send all human-facing output to stderr (used in machine/JSON mode)."""
        self._out = _err_console

    def banner(self, version: str) -> None:
        _err_console.print(f"\n[bold #d8b4fe]{_banner_text()}[/bold #d8b4fe]\n")
        meta = Text.assemble((f" v{version} ", "bold #d8b4fe"), "  ", (f" {_REPO_URL} ", "dim italic"))
        _err_console.print(Rule(title=meta, style="#d8b4fe", align="left"))
        _err_console.print()

    def info(self, msg: str) -> None:
        self._out.print(f"{_PREFIX} {msg}")

    def success(self, msg: str) -> None:
        self._out.print(f"{_PREFIX} [#86efac]✓ {msg}[/#86efac]")

    def warn(self, msg: str) -> None:
        self._out.print(f"{_PREFIX} [#fcd34d]⚠ {msg}[/#fcd34d]")

    def warn_stderr(self, msg: str) -> None:
        """Emit a human-facing warning on stderr without changing the route."""
        _err_console.print(f"{_PREFIX} [#fcd34d]⚠ {msg}[/#fcd34d]")

    def note_stderr(self, msg: str) -> None:
        """Emit a neutral note on stderr (used by the pre-command synchronizer),
        so stdout stays clean even for non-JSON commands."""
        _err_console.print(f"{_PREFIX} [dim]◈ {msg}[/dim]")

    def error(self, msg: str) -> None:
        self._out.print(f"{_PREFIX} [bold #fda4af]✗ {msg}[/bold #fda4af]")

    def step(self, msg: str) -> None:
        self._out.print(f"{_PREFIX} [dim]›[/dim] {msg}")

    def header(self, title: str) -> None:
        self._out.print()
        self._out.print(Rule(title=f"[bold]{title}[/bold]", style="#d8b4fe", align="left"))

    def key_value(self, data: dict[str, str], title: str = "") -> None:
        table = Table(show_header=False, border_style="dim", padding=(0, 2), title=title or None)
        table.add_column("Key", style="bold", no_wrap=True)
        table.add_column("Value")
        for key, value in data.items():
            table.add_row(key, value)
        self._out.print(table)

    def results_table(self, rows: list[tuple[str, str, str]], title: str = "") -> None:
        table = Table(border_style="dim", padding=(0, 1), title=title or None)
        table.add_column("Name", style="bold", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Detail")
        styles = {
            "ok": "#86efac",
            "done": "#86efac",
            "success": "#86efac",
            "error": "#fda4af",
            "failed": "#fda4af",
            "blocked": "#fcd34d",
            "pending": "dim",
            "skipped": "dim",
        }
        for name, status, detail in rows:
            style = styles.get(status.lower(), "")
            styled = f"[{style}]{status}[/{style}]" if style else status
            table.add_row(name, styled, detail)
        self._out.print(table)

    def panel(self, content: str, title: str = "", style: str = "#d8b4fe") -> None:
        self._out.print(Panel(content, title=title or None, border_style=style, padding=(1, 2)))


log = HorizonLog()
