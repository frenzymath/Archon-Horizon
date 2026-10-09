"""Run the same CLI as the ``horizon`` and ``horizon-pipeline`` console scripts.

Keeping one implementation avoids command behavior drifting between entrypoints.
SystemExit below propagates the CLI's return code to shells and service managers.
"""

from __future__ import annotations

from .pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
