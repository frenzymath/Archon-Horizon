"""Agent prompt/output contract: prompts expose schema, parser consumes it."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.agents.base import HorizonContext, InformalContext
from archon_horizon.agents.parsing import parse_informal_update
from archon_horizon.agents.prompts import compose_horizon_prompt, compose_informal_prompt
from archon_horizon.core.inbox import InboxItem, InboxKind
from archon_horizon.core.labels import ARCHON_ACCEPT, ARCHON_PENDING
from archon_horizon.core.roadmap import Roadmap, RoadmapStatus
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask, WriteSet
from archon_horizon.core.workspace import Project, Workspace


def _workspace(tmp_path: Path) -> Workspace:
    return Workspace(
        name="ws",
        root=tmp_path,
        projects={"ag-main": Project(name="ag-main", path=Path("projects/ag-main"), build_command="lake build")},
    )


def test_informal_prompt_contains_archon_horizon_contract(tmp_path: Path) -> None:
    run = RunRecord(id="S-1", rounds_requested=1)
    ctx = InformalContext(
        workspace=_workspace(tmp_path),
        run=run,
        focus=run.focus,
        roadmap=Roadmap(),
        accepted_inbox=(InboxItem(id="I-1", provider="local", kind=InboxKind.HINT, body="use affine", labels=(ARCHON_ACCEPT,)),),
        blueprint_summary="ag-main: 1 nodes, 0 proved, 0 edges, 0 dangling",
        memory="avoid old lemma",
    )

    prompt = compose_informal_prompt(ctx)

    assert "informal agent" in prompt
    assert "Required structured output contract" in prompt
    assert '"tasks"' in prompt and '"roadmap"' in prompt and '"local_issues"' in prompt
    assert "ag-main: 1 nodes" in prompt
    assert "use affine" in prompt


def test_horizon_prompt_is_task_scoped(tmp_path: Path) -> None:
    task = HorizonTask(
        id="T-1",
        project="ag-main",
        objective="Prove Foo.bar",
        write_set=WriteSet(files=("Foo.lean",)),
        roadmap_refs=("R-1",),
    )
    ctx = HorizonContext(
        workspace=_workspace(tmp_path),
        run=RunRecord(id="S-1", rounds_requested=1),
        task=task,
        roadmap=Roadmap(),
    )

    prompt = compose_horizon_prompt(ctx)

    assert "Complete exactly one assigned" in prompt
    assert "Prove Foo.bar" in prompt
    assert "files=Foo.lean" in prompt
    assert "roadmap=R-1" in prompt


def test_parse_informal_update_accepts_first_version_schema() -> None:
    update = parse_informal_update(
        """Report body.
```json
{
  "memory": "new memory",
  "roadmap": {"items": [{"id": "R-1", "title": "Do the thing", "projects": ["ag-main"], "status": "active"}]},
  "tasks": [{"project": "ag-main", "objective": "prove x", "write_set": {"files": ["X.lean"]}, "roadmap_refs": ["R-1"]}],
  "proposals": [{"title": "Split project", "body": "Make a base project", "project": "ag-main", "metadata": {"kind": "split"}}],
  "local_issues": [{"kind": "question", "body": "Which source proves this?", "project": "ag-main"}]
}
```
"""
    )

    assert update.report == "Report body."
    assert update.memory == "new memory"
    assert update.roadmap is not None
    assert update.roadmap.items[0].status is RoadmapStatus.ACTIVE
    assert update.tasks[0].write_set.files == ("X.lean",)
    assert update.tasks[0].roadmap_refs == ("R-1",)
    assert update.proposals[0].metadata["kind"] == "split"
    assert update.local_issues[0].labels == (ARCHON_PENDING,)
    assert update.local_issues[0].scope.project == "ag-main"
