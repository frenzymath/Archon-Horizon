"""The trimmed inbox taxonomy: kinds, status, and author provenance."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.cli import main
from archon_horizon.core.inbox import InboxKind, InboxStatus
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


def test_kind_and_status_sets() -> None:
    assert {k.value for k in InboxKind} == {"hint", "issue", "protection", "info", "memory"}
    assert {s.value for s in InboxStatus} == {"open", "closed", "archived"}


def test_add_records_author(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    assert main(["--root", str(ws), "inbox", "add", "--body", "x\n\nbody", "--author", "horizon"]) == 0
    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.author == "horizon"


def test_archive_is_a_soft_delete(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    assert main(["--root", str(ws), "inbox", "add", "--body", "x\n\nbody", "--author", "ground"]) == 0
    assert main(["--root", str(ws), "inbox", "archive", "I-0001"]) == 0
    provider = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local")
    item = provider.get_item("I-0001")
    # Archived, not deleted: still present, distinct from closed.
    assert item.status is InboxStatus.ARCHIVED
    assert any(i.id == "I-0001" for i in provider.list_items())


def test_add_uses_agent_role_env_as_default_author(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")

    assert main(["--root", str(ws), "inbox", "add", "--body", "x\n\nbody"]) == 0

    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.author == "horizon"


def test_subagent_author_is_canonicalized_to_role(tmp_path: Path, monkeypatch) -> None:
    # A subagent that types its own descriptor name into --author must not leak it
    # into the author field: the author stays the dispatching role, and the name is
    # preserved as the `agent` metadata detail.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")

    assert main([
        "--root", str(ws), "inbox", "add", "--body", "x\n\nbody", "--author", "blueprint-reviewer",
    ]) == 0

    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.author == "horizon"
    assert item.metadata.get("agent") == "blueprint-reviewer"


def test_agent_flag_records_subidentity(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")

    assert main([
        "--root", str(ws), "inbox", "add", "--body", "x\n\nbody", "--agent", "diff-auditor",
    ]) == 0

    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.author == "horizon"
    assert item.metadata.get("agent") == "diff-auditor"


def test_agent_detail_strips_redundant_role_prefix(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")

    assert main([
        "--root", str(ws), "inbox", "add", "--body", "x\n\nbody", "--author", "horizon-diff-auditor",
    ]) == 0

    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.author == "horizon"
    assert item.metadata.get("agent") == "diff-auditor"


def test_human_cli_author_is_respected_without_agent_role(tmp_path: Path) -> None:
    # No agent role in the environment → a direct human caller keeps their author,
    # and no `agent` metadata is invented.
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    assert main(["--root", str(ws), "inbox", "add", "--body", "x\n\nbody", "--author", "human"]) == 0
    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert item.author == "human"
    assert "agent" not in item.metadata


def test_add_stamps_run_session_provenance(tmp_path: Path, monkeypatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    monkeypatch.setenv("ARCHON_HORIZON_AGENT_ROLE", "horizon")
    monkeypatch.setenv("ARCHON_HORIZON_RUN", "0003")
    monkeypatch.setenv("ARCHON_HORIZON_SESSION", "0002-horizon")

    assert main(["--root", str(ws), "inbox", "add", "--body", "title\n\ndescription"]) == 0

    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    prov = item.metadata["provenance"]
    assert prov == {"run": "0003", "session": "0002-horizon", "role": "horizon"}


def test_add_without_run_env_has_no_provenance(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "config.yaml").write_text(_CONFIG, "utf-8")
    assert main(["--root", str(ws), "inbox", "add", "--body", "title\n\ndescription", "--author", "human"]) == 0
    item = FilesystemInboxProvider(ws / ".archon-horizon" / "inbox" / "local").get_item("I-0001")
    assert "provenance" not in item.metadata
