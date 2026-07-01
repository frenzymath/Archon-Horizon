"""Protection items: the soft 'freeze' modeled as persistent inbox constraints."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.agents.base import HorizonContext
from archon_horizon.agents.prompts import compose_horizon_prompt
from archon_horizon.cli import main
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxScope
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.sessions import RunRecord
from archon_horizon.core.tasks import HorizonTask
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

_CONFIG = """
workspace:
  name: w
  rounds: 1
  ground_agent: {harness: inf}
  horizon_agent: {harness: hor}
harnesses:
  inf: {kind: "null"}
  hor: {kind: "null"}
projects:
  ag-main: {path: projects/ag-main}
"""


def test_protect_creates_persistent_protection_item(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    rc = main([
        "--root", str(ws), "inbox", "protect",
        "--body", "do not change the signature of Foo.bar",
        "--declaration", "Foo.bar",
    ])
    assert rc == 0
    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.kind is InboxKind.PROTECTION
    assert item.body.startswith("[persistent]")
    assert item.scope.targets("declarations") == ("Foo.bar",)
    assert item.audience == "horizon"


def test_horizon_prompt_renders_protected_section(tmp_path: Path) -> None:
    protection = InboxItem(
        id="I-1", provider="local", kind=InboxKind.PROTECTION,
        body="do not change the signature of Foo.bar", labels=(),
        scope=InboxScope(declarations=("Foo.bar",)), audience="horizon",
    )
    ctx = HorizonContext(
        workspace=Workspace(name="w", root=tmp_path, projects={"p": Project(name="p", path=Path("projects/p"))}),
        run=RunRecord(id="S", rounds_requested=1),
        task=HorizonTask(id="T", project="p", objective="do x"),
        roadmap=Roadmap(),
        accepted_inbox=(protection,),
    )
    prompt = compose_horizon_prompt(ctx)
    assert "# Protected" in prompt
    assert "do not change the signature of Foo.bar" in prompt
    assert "decls=Foo.bar" in prompt
    # The protection item is not duplicated in the general opened-inbox list.
    assert prompt.count("do not change the signature of Foo.bar") == 1
