"""Every shipped Python entrypoint must import without retired runtime modules."""

from __future__ import annotations

import importlib
from pathlib import Path

import archon_horizon


def test_all_shipped_modules_import():
    package = Path(archon_horizon.__file__).parent
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package)
        if "frontend" in relative.parts:
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        importlib.import_module(".".join(["archon_horizon", *parts]))
