#!/usr/bin/env python3
"""Assert the built wheel ships exactly the data files we intend.

``include-package-data`` is deliberately OFF in ``pyproject.toml`` because it
swept the whole package tree and pulled hundreds of ``frontend/node_modules``
files into the wheel. That makes the wheel's contents a *configured* list which
can silently drift, in either direction:

* a MISSING ``frontend/dist`` breaks the dashboard for pip users;
* a REAPPEARING ``node_modules`` bloats the wheel by tens of MB.

Neither shows up in the test suite, so check it in CI instead.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# (description, predicate) — every one of these must match >=1 wheel entry.
REQUIRED = [
    ("milestone contract guide", lambda n: n == "archon_horizon/pipeline/skills/operations/horizon-graph/references/milestones.md"),
    ("milestone host checker", lambda n: n == "archon_horizon/pipeline/worker/milestone_verify.py"),
    ("pipeline entry skill", lambda n: n == "archon_horizon/pipeline/skills/operations/horizon-pipeline/SKILL.md"),
    ("pipeline reviewer skill", lambda n: n == "archon_horizon/pipeline/skills/review/horizon-review/SKILL.md"),
    ("review measurement helper", lambda n: n == "archon_horizon/pipeline/skills/lean/lean-performance/scripts/compare_measurements.py"),
    ("library audit skill", lambda n: n == "archon_horizon/pipeline/skills/review/library-audit/SKILL.md"),
    ("reviewer descriptor", lambda n: n == "archon_horizon/pipeline/subagents/reviewers/library-api.md"),
    ("worker specialist", lambda n: n == "archon_horizon/pipeline/subagents/implementation/lean-worker.md"),
    ("research specialist", lambda n: n == "archon_horizon/pipeline/subagents/research/page-transcriber.md"),
    ("validation specialist", lambda n: n == "archon_horizon/pipeline/subagents/validation/build-checker.md"),
    ("planning specialist", lambda n: n == "archon_horizon/pipeline/subagents/planning/graph-planner.md"),
    ("adapted Lean skill license", lambda n: n == "archon_horizon/pipeline/skills/_sources/lean4-skills/LICENSE.md"),
    ("pipeline review guidance", lambda n: n == "archon_horizon/pipeline/skills/lean/horizon-formalization/references/review-guidance.md"),
    ("pipeline migration runtime", lambda n: n == "archon_horizon/pipeline/migrations/env.py"),
    ("built dashboard assets", lambda n: n.startswith("archon_horizon/frontend/dist/")),
    ("dashboard entry point", lambda n: n == "archon_horizon/frontend/dist/index.html"),
    ("project notice", lambda n: n.endswith(".dist-info/licenses/NOTICE")),
    ("third-party notices", lambda n: n.endswith(".dist-info/licenses/THIRD_PARTY_NOTICES.md")),
]

# (description, predicate) — no wheel entry may match any of these.
FORBIDDEN = [
    ("retired runtime", lambda n: n.split("/")[:2] in [
        ["archon_horizon", package] for package in (
            "agents", "blueprint", "config", "core", "harnesses", "hgraph",
            "inboxes", "orchestration", "render", "runlog.py", "server",
            "store", "transcript", "vcs", "platform", "commands", "skills",
            "subagents", "lean", "cli.py", "worker_cli.py", "log.py",
        )
    ]),
    ("duplicate instruction catalogs", lambda n: "/skills/control_plane/" in n
     or "/subagents/control_plane/" in n or "/subagents/descriptors/" in n),
    ("frontend node_modules", lambda n: "node_modules/" in n),
    ("frontend sources", lambda n: n.startswith("archon_horizon/frontend/src/")),
    ("frontend toolchain config", lambda n: n.rsplit("/", 1)[-1] in {"vite.config.ts", "tsconfig.json", "package-lock.json"}),
    ("tests", lambda n: n.startswith("tests/") or n.startswith("archon_horizon/tests/")),
    ("demo workspace", lambda n: n.startswith("demo/") or "/demo/" in n),
    ("workspace state", lambda n: ".archon-horizon/" in n),
    ("local configuration", lambda n: n.rsplit("/", 1)[-1] in {".mcp.json", ".env", "config.yaml"}
     or any(part in {".claude", ".codex", ".horizon", ".horizon-agent-library", "agent-library", "lean-cache"}
            for part in n.split("/"))),
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
        stale = [name for name in names if name.startswith("archon_horizon/")
                 and not name.endswith("/")
                 and (not (root / "src" / name).is_file()
                      or (root / "src" / name).read_bytes() != zf.read(name))]

    failures: list[str] = []
    if stale:
        failures.append(f"stale build artifacts ({len(stale)} entries, e.g. {', '.join(stale[:3])}); rebuild from a clean build directory")
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
