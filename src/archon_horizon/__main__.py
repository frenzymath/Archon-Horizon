"""``python -m archon_horizon`` entrypoint."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    """Console-script entry that can short-circuit the heavy CLI import.

    Engine lifecycle hooks call ``horizon agent-hook-fast`` on every tool
    boundary. Importing the full Typer app for that path costs hundreds of
    milliseconds; route it before loading ``archon_horizon.cli``.
    """
    args = list(argv) if argv is not None else sys.argv[1:]
    if args and args[0] == "agent-hook-fast":
        # Preserve --root / other flags on sys.argv for main_fast's resolver.
        if argv is not None:
            sys.argv = [sys.argv[0] if sys.argv else "horizon", *args]
        from archon_horizon.commands.agent_hook import main_fast

        return main_fast(args[1:])
    from archon_horizon.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
