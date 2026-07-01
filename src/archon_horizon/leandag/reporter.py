"""Output abstraction: pretty (rich) for humans, plain/JSON for machines.

Every command writes through a :class:`Reporter` instead of touching ``rich``
directly, so the same code path can serve an interactive terminal or an LLM
agent driving the CLI. Three formats:

- ``rich`` — styled console output (default, for humans).
- ``text`` — plain text, no colour/box-drawing; tables render as aligned rows.
- ``json`` — structured JSON on stdout (the machine-friendly contract).

Progress/diagnostic lines (``step``/``ok``/``warn`` …) go to **stderr** in the
machine formats, so stdout carries only the structured payload emitted by
:meth:`emit`. That lets an agent capture clean JSON from stdout while still
seeing progress on stderr.

``rich`` is imported lazily and only for the ``rich`` format — the ``text`` and
``json`` paths have no third-party dependency, so an agent can drive the CLI in
a minimal environment.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Callable, Optional, Sequence


class Reporter:
    def __init__(self, fmt: str = "rich") -> None:
        if fmt not in ("rich", "text", "json"):
            fmt = "rich"
        self.fmt = fmt
        self._out_console = None
        self._err_console = None

    @property
    def machine(self) -> bool:
        return self.fmt != "rich"

    def _console(self, stderr: bool = False):
        from rich.console import Console  # lazy: only the rich path needs it
        if stderr:
            if self._err_console is None:
                self._err_console = Console(stderr=True)
            return self._err_console
        if self._out_console is None:
            self._out_console = Console()
        return self._out_console

    # ── Progress / diagnostics (stderr in machine modes) ─────────────────────

    def rule(self, title: str) -> None:
        if self.machine:
            self._plain(f"== {title} ==")
        else:
            from rich.rule import Rule
            c = self._console()
            c.print()
            c.print(Rule(f"[bold]{title}[/bold]", style="dim"))
            c.print()

    def step(self, msg: str) -> None:
        self._plain(f"- {msg}") if self.machine else self._console().print(f"  [dim]•[/dim] {msg}")

    def detail(self, msg: str) -> None:
        self._plain(f"  {msg}") if self.machine else self._console().print(f"    [dim]{msg}[/dim]")

    def ok(self, msg: str) -> None:
        self._plain(f"OK: {msg}") if self.machine else self._console().print(f"  [green]✓[/green] {msg}")

    def warn(self, msg: str) -> None:
        self._plain(f"WARN: {msg}") if self.machine else self._console().print(f"  [yellow]![/yellow] {msg}")

    def error(self, msg: str) -> None:
        if self.machine:
            self._plain(f"ERROR: {msg}")
        else:
            self._console(stderr=True).print(f"  [red]✗[/red] {msg}")

    def blank(self) -> None:
        if not self.machine:
            self._console().print()

    def _plain(self, line: str) -> None:
        print(line, file=sys.stderr)

    # ── Structured payloads (stdout) ─────────────────────────────────────────

    def emit(
        self,
        obj: Any,
        *,
        rich: Optional[Callable[[], None]] = None,
        text: Optional[Callable[[], None]] = None,
    ) -> None:
        """Emit the command's data. In ``json`` mode prints ``obj`` as JSON; in
        ``text``/``rich`` mode calls the matching renderer (falling back to a
        JSON dump if none is given)."""
        dump = lambda: print(_json.dumps(obj, indent=2, ensure_ascii=False))
        if self.fmt == "json":
            dump()
        elif self.fmt == "text":
            (text or rich or dump)()
        else:
            (rich or text or dump)()

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
        """Render a table to stdout in the current format. (In ``json`` mode the
        caller emits the underlying object via :meth:`emit` instead.)"""
        if self.fmt == "rich":
            from rich import box
            from rich.table import Table
            t = Table(box=box.SIMPLE, show_header=True, padding=(0, 1))
            for h in headers:
                t.add_column(str(h))
            for r in rows:
                t.add_row(*(str(c) for c in r))
            self._console().print(t)
            return
        # text / json fallback: aligned plain-text columns
        cells = [[str(c) for c in r] for r in rows]
        widths = [
            max(len(str(headers[i])), *(len(row[i]) for row in cells)) if cells else len(str(headers[i]))
            for i in range(len(headers))
        ]
        line = lambda vals: "  ".join(str(v).ljust(widths[i]) for i, v in enumerate(vals))
        print(line(headers))
        print("  ".join("-" * w for w in widths))
        for row in cells:
            print(line(row))
