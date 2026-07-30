"""Seed the demo-only workspace ledger with two provenance-tagged commits.

The source fixture stays at its final state. During each commit, the Lean file
is briefly rewound to the Chapter 1 frontier so the static Logs view has a
realistic two-commit history to inspect.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from archon_horizon.vcs.git import WorkspaceGit


def _pin_commits(state: Path, pins: dict[str, list[str]]) -> None:
    """Write ``pinned_commits`` into each roadmap item's metadata in place.

    Rewrites only that one key so the rest of the checked-in fixture (ordering,
    comments, formatting of other fields) is preserved as authored.
    """
    import yaml

    for item_id, shas in pins.items():
        path = state / "roadmap" / "items" / f"{item_id}.yaml"
        data = yaml.safe_load(path.read_text("utf-8"))
        data.setdefault("metadata", {})["pinned_commits"] = list(shas)
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")


def main() -> None:
    root = Path(__file__).resolve().parent
    state = root / ".archon-horizon"
    shutil.rmtree(state / "vcs", ignore_errors=True)
    shutil.rmtree(state / "cache", ignore_errors=True)

    lean_path = root / "MiniTopology" / "MiniTopology.lean"
    lean_final = lean_path.read_text("utf-8")
    marker = "\n/- This is intentionally unfinished"
    if marker not in lean_final:
        raise RuntimeError("demo Lean fixture is missing its Chapter 2 marker")
    lean_chapter1 = lean_final.split(marker, 1)[0].rstrip() + "\n\nend MiniTopology\n"

    env = {
        "GIT_AUTHOR_NAME": "Archon Horizon demo",
        "GIT_AUTHOR_EMAIL": "demo@archon-horizon.invalid",
        "GIT_COMMITTER_NAME": "Archon Horizon demo",
        "GIT_COMMITTER_EMAIL": "demo@archon-horizon.invalid",
        "GIT_AUTHOR_DATE": "2026-07-18T08:01:00+00:00",
        "GIT_COMMITTER_DATE": "2026-07-18T08:01:00+00:00",
    }
    os.environ.update(env)
    git = WorkspaceGit(root)
    git.init()
    baseline = git.commit(
        "demo: initialize the miniature workspace",
        paths=[
            "config.yaml",
            "MiniTopology/hgraph/config.yaml",
            "MiniTopology/blueprint/src/content.tex",
        ],
    )

    os.environ["GIT_AUTHOR_DATE"] = "2026-07-18T08:11:00+00:00"
    os.environ["GIT_COMMITTER_DATE"] = "2026-07-18T08:11:00+00:00"
    lean_path.write_text(lean_chapter1, "utf-8")
    try:
        first = git.commit(
            "demo: establish Chapter 1 foundations",
            paths=["MiniTopology/MiniTopology.lean", "MiniTopology/blueprint/src/chapters/chapter1.tex"],
            trailers={
                "Archon-Run": "0001",
                "Archon-Session": "0001-horizon-DEMO.1",
                "Archon-Role": "horizon",
                "Archon-Commit": "agent",
            },
        )
    finally:
        lean_path.write_text(lean_final, "utf-8")

    os.environ["GIT_AUTHOR_DATE"] = "2026-07-19T09:18:00+00:00"
    os.environ["GIT_COMMITTER_DATE"] = "2026-07-19T09:18:00+00:00"
    second = git.commit(
        "demo: record the partial connectedness frontier",
        paths=["MiniTopology/MiniTopology.lean", "MiniTopology/blueprint/src/chapters/chapter2.tex"],
        trailers={
            "Archon-Run": "0002",
            "Archon-Session": "0001-horizon-DEMO.2",
            "Archon-Role": "horizon",
            "Archon-Commit": "agent",
        },
    )
    if not baseline or not first or not second:
        raise RuntimeError("demo ledger seeding did not create all three commits")

    # Pin each commit to the roadmap item it delivered, so the board's Commits
    # row is populated. This happens here rather than in the checked-in YAML
    # because the SHAs are regenerated on every run — hardcoding them would go
    # stale the first time this script changes.
    _pin_commits(state, {"M-FOUND": [first], "M-CONN": [second]})
    print(f"demo commits: {baseline} {first} {second}")


if __name__ == "__main__":
    main()
