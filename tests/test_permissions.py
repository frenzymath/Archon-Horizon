"""Write-permission domains: glob coverage, default lanes, and enforcement."""

from __future__ import annotations

import subprocess
from pathlib import Path

from archon_horizon.config.loader import build_orchestrator
from archon_horizon.core.permissions import (
    WriteDomain,
    glob_covers,
    horizon_write_domain,
    ground_write_domain,
)
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapStatus
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.harnesses.base import HarnessResult
from archon_horizon.harnesses.null import NullHarness
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider


def _ws(tmp_path: Path) -> Workspace:
    return Workspace(
        name="w",
        root=tmp_path,
        projects={
            "a": Project(name="a", path=Path("projects/a")),
            "b": Project(name="b", path=Path("projects/b"), write_paths=("shared/**",)),
            "c": Project(name="c", path=Path("projects/c"), depends_on=("a", "b")),
        },
    )


def test_glob_covers_handles_trees_and_patterns() -> None:
    assert glob_covers("projects/a/**", "projects/a/Foo.lean")
    assert glob_covers("projects/a/**", "projects/a")
    assert not glob_covers("projects/a/**", "projects/b/Foo.lean")
    assert glob_covers("*.lean", "Foo.lean")
    assert glob_covers(".archon-horizon/memory.md", ".archon-horizon/memory.md")


def test_horizon_domain_is_project_tree(tmp_path: Path) -> None:
    domain = horizon_write_domain(_ws(tmp_path), ("a",))
    assert domain.covers("projects/a/Foo.lean")
    assert domain.covers("references/foo.pdf")                # shared library is writable
    assert not domain.covers("projects/b/Bar.lean")          # another project
    assert not domain.covers(".archon-horizon/roadmap/items/A.3.yaml")  # Ground-only
    assert not domain.covers(".archon-horizon/memory.md")     # memory is via the inbox now


def test_horizon_domain_spans_several_projects(tmp_path: Path) -> None:
    # A task may span projects; Horizon's lane is their union (+ references).
    domain = horizon_write_domain(_ws(tmp_path), ("a", "b"))
    assert domain.covers("projects/a/Foo.lean")
    assert domain.covers("projects/b/Bar.lean")
    assert domain.covers("references/paper.tex")


def test_ground_domain_is_wider(tmp_path: Path) -> None:
    domain = ground_write_domain(_ws(tmp_path), ("a", "b"))
    assert domain.covers(".archon-horizon/roadmap/items/A.3.yaml")
    assert domain.covers("projects/a/X.lean")
    assert domain.covers("projects/b/Y.lean")


def test_write_paths_extend_the_lane(tmp_path: Path) -> None:
    domain = horizon_write_domain(_ws(tmp_path), ("b",))
    assert domain.covers("shared/util.lean")  # from Project.write_paths


def test_project_dependencies_extend_the_lane(tmp_path: Path) -> None:
    domain = horizon_write_domain(_ws(tmp_path), ("c",))
    assert domain.covers("projects/c/Main.lean")
    assert domain.covers("projects/a/Shared.lean")
    assert domain.covers("projects/b/Shared.lean")
    assert domain.covers("shared/util.lean")


def test_violations_lists_uncovered_paths() -> None:
    domain = WriteDomain(("projects/a/**",))
    assert domain.violations(("projects/a/F.lean", "projects/b/G.lean")) == ("projects/b/G.lean",)


_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf, subagents: []}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
  other: {path: projects/other}
"""


def test_orchestrator_flags_out_of_scope_horizon_writes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "projects" / "ag-main").mkdir(parents=True)
    (root / "projects" / "other").mkdir(parents=True)
    (root / "config.yaml").write_text(_CONFIG, "utf-8")
    # Seed the out-of-tree workspace git so the orchestrator can diff what the
    # session newly changed (write-domain detection needs a baseline).
    from archon_horizon.vcs.git import WorkspaceGit

    wsgit = WorkspaceGit(root)
    wsgit.init()
    wsgit.commit("init")

    def sneaky(req: HarnessResult) -> HarnessResult:
        # Horizon is assigned ag-main but writes into another project's tree.
        (root / "projects" / "other" / "Sneaky.lean").write_text("def x := 1\n", "utf-8")
        return HarnessResult(ok=True, text="done")

    local = FilesystemInboxProvider(root / ".archon-horizon" / "inbox" / "local")
    orch = build_orchestrator(
        root, harnesses={"inf": NullHarness(""), "hor": NullHarness(sneaky)}, inbox_providers=[local]
    )
    orch.roadmap_store.save(
        Roadmap(items=(RoadmapItem(id="R-1", title="x", projects=("ag-main",), status=RoadmapStatus.ACTIVE),))
    )

    orch.run(RunRecord(id="", rounds_requested=1))

    assert local.list_items() == []
    event = next(e for e in orch.event_log.read_all() if e.type == "write_domain.violation")
    assert "projects/other/Sneaky.lean" in event.data["paths"]
