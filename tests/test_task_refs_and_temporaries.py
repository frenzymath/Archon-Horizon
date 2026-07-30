"""`task set` ref-linking (I-0390/I-0380) and [temporary] auto-archive."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

from archon_horizon.cli import main
from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxStatus
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.orchestration.orchestrator import Orchestrator

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
"""


def test_task_set_links_refs(tmp_path: Path, capsys) -> None:
    ws = tmp_path / "ws"
    (ws / "projects" / "ag-main").mkdir(parents=True)
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    main(["--root", str(ws), "task", "add", "--id", "T-1", "--project", "ag-main", "--objective", "do it"])
    capsys.readouterr()
    # The reported gap: refs were unsettable via the CLI (I-0390/I-0380).
    main(["--root", str(ws), "task", "set", "T-1",
          "--roadmap-ref", "A.1", "--roadmap-ref", "A.2", "--inbox-ref", "I-0003", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["roadmap_refs"] == ["A.1", "A.2"]
    assert payload["inbox_refs"] == ["I-0003"]


def test_archive_consumed_temporaries(tmp_path: Path) -> None:
    inbox = FilesystemInboxProvider(tmp_path / "inbox" / "local")
    old_tmp = inbox.create_item(InboxDraft(kind=InboxKind.HINT, body="[temporary] old\n\nbody"))
    old_persistent = inbox.create_item(InboxDraft(kind=InboxKind.HINT, body="[persistent] keep\n\nbody"))
    old_plain = inbox.create_item(InboxDraft(kind=InboxKind.HINT, body="plain\n\nbody"))
    time.sleep(0.005)
    before = utc_now()  # run boundary: everything above predates it
    time.sleep(0.005)
    fresh_tmp = inbox.create_item(InboxDraft(kind=InboxKind.HINT, body="[temporary] fresh\n\nbody"))

    events: list = []
    fake = SimpleNamespace(inbox_providers=[inbox], _emit=lambda *a, **k: events.append((a, k)))
    Orchestrator._archive_consumed_temporaries(fake, before)

    # Only the consumed [temporary] leftover is archived.
    assert inbox.get_item(old_tmp.id).status is InboxStatus.ARCHIVED
    assert inbox.get_item(old_persistent.id).status is InboxStatus.OPEN   # not temporary
    assert inbox.get_item(old_plain.id).status is InboxStatus.OPEN        # not temporary
    assert inbox.get_item(fresh_tmp.id).status is InboxStatus.OPEN        # created this run
    assert events and events[0][1]["items"] == [old_tmp.id]
