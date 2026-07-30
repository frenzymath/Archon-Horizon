"""Protection items: the soft 'freeze' modeled as persistent inbox constraints."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import InboxItem, InboxKind, InboxScope
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
        "--blueprint-node", "thm:foo-bar",
    ])
    assert rc == 0
    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.kind is InboxKind.PROTECTION
    assert item.body.startswith("[persistent]")
    assert item.scope.targets("declarations") == ("Foo.bar",)
    assert item.scope.targets("blueprint_nodes") == ("thm:foo-bar",)
    assert item.audience == "horizon"


def test_protection_reaches_horizon_via_inbox_pull(tmp_path: Path) -> None:
    # Protections are no longer pushed through the prompt: the agent pulls them
    # with `horizon inbox list` (the skill says to). This pins the pull path —
    # the item is open, agent-ready, and addressed so it reaches horizon.
    from archon_horizon.core.inbox import reaches_horizon
    from archon_horizon.core.labels import is_agent_ready

    protection = InboxItem(
        id="I-1", provider="local", kind=InboxKind.PROTECTION,
        body="do not change the signature of Foo.bar", labels=("agent-ready",),
        scope=InboxScope(declarations=("Foo.bar",)), audience="horizon",
    )
    assert is_agent_ready(protection.labels)
    assert reaches_horizon(protection, "p")
