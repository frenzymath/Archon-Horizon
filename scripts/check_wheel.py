#!/usr/bin/env python3
"""Assert the built wheel ships exactly the data files we intend.

``include-package-data`` is deliberately OFF in ``pyproject.toml`` because it
swept the whole package tree and pulled hundreds of ``frontend/node_modules``
files into the wheel. That makes the wheel's contents a *configured* list which
can silently drift, in either direction:

* a MISSING ``frontend/dist`` breaks ``horizon dashboard`` for pip users;
* a REAPPEARING ``node_modules`` bloats the wheel by tens of MB.

Neither shows up in the test suite, so check it in CI instead.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# (description, predicate) — every one of these must match >=1 wheel entry.
REQUIRED = [
    ("bundled skills", lambda n: n.startswith("archon_horizon/skills/") and n.endswith(".md")),
    ("subagent descriptors", lambda n: "/subagents/" in n and n.endswith(".md")),
    ("hgraph docs", lambda n: n.startswith("archon_horizon/hgraph/") and n.endswith(".md")),
    ("built dashboard assets", lambda n: n.startswith("archon_horizon/frontend/dist/")),
    ("dashboard entry point", lambda n: n == "archon_horizon/frontend/dist/index.html"),
]

# (description, predicate) — no wheel entry may match any of these.
FORBIDDEN = [
    ("frontend node_modules", lambda n: "node_modules/" in n),
    ("frontend sources", lambda n: n.startswith("archon_horizon/frontend/src/")),
    ("frontend toolchain config", lambda n: n.rsplit("/", 1)[-1] in {"vite.config.ts", "tsconfig.json", "package-lock.json"}),
    ("tests", lambda n: n.startswith("tests/") or n.startswith("archon_horizon/tests/")),
    ("demo workspace", lambda n: n.startswith("demo/") or "/demo/" in n),
    ("workspace state", lambda n: ".archon-horizon/" in n),
    ("caches", lambda n: "__pycache__/" in n or n.endswith(".pyc")),
]


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    wheels = sorted((root / "dist").glob("*.whl"))
    if not wheels:
        print("error: no wheel found under dist/ — run `python -m build` first", file=sys.stderr)
        return 1
    wheel = wheels[-1]
    with zipfile.ZipFile(wheel) as zf:
        names = zf.namelist()

    failures: list[str] = []
    for label, predicate in REQUIRED:
        if not any(predicate(n) for n in names):
            failures.append(f"missing: {label}")
    for label, predicate in FORBIDDEN:
        hits = [n for n in names if predicate(n)]
        if hits:
            sample = ", ".join(hits[:3])
            failures.append(f"unexpected {label} ({len(hits)} entries, e.g. {sample})")

    print(f"checked {wheel.name}: {len(names)} entries")
    if failures:
        for failure in failures:
            print(f"  ✗ {failure}", file=sys.stderr)
        return 1
    print("  ✓ wheel contents look right")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
