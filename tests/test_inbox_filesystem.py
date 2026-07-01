"""Filesystem local inbox: CRUD, persistence, labels, and accepted filtering."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.core.inbox import InboxDraft, InboxFilter, InboxKind, InboxScope, InboxStatus
from archon_horizon.core.labels import NOT_READY, REJECTED, is_agent_ready
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider


def _provider(tmp_path: Path) -> FilesystemInboxProvider:
    return FilesystemInboxProvider(tmp_path / ".archon-horizon" / "inbox" / "local")


def test_create_defaults_to_accepted_and_persists(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    created = provider.create_item(
        InboxDraft(kind=InboxKind.HINT, body="Try the affine case first.")
    )
    assert created.id == "I-0001"
    assert is_agent_ready(created.labels)

    # A fresh provider over the same file sees the persisted item.
    reloaded = FilesystemInboxProvider(provider._path).get_item("I-0001")  # noqa: SLF001
    assert reloaded.body == "Try the affine case first."


def test_ids_increment_and_filter_by_kind_and_project(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider.create_item(InboxDraft(kind=InboxKind.HINT, body="h", scope=InboxScope(projects=("a",))))
    second = provider.create_item(
        InboxDraft(kind=InboxKind.INFO, body="p", scope=InboxScope(projects=("b",)))
    )
    assert second.id == "I-0002"

    notices = provider.list_items(InboxFilter(kinds=(InboxKind.INFO,)))
    assert [i.id for i in notices] == ["I-0002"]
    assert provider.list_items(InboxFilter(project="a"))[0].id == "I-0001"


def test_relabel_pending_makes_it_unaccepted(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(InboxDraft(kind=InboxKind.ISSUE, body="x"))
    provider.update_labels(item.id, [NOT_READY])
    assert not is_agent_ready(provider.get_item(item.id).labels)


def test_status_comment_and_delete(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="x"))

    provider.update_status(item.id, InboxStatus.CLOSED)
    assert provider.get_item(item.id).status is InboxStatus.CLOSED

    provider.add_comment(item.id, "done in T-0007")
    assert provider.get_item(item.id).metadata["comments"][0]["body"] == "done in T-0007"

    provider.delete_item(item.id)
    assert provider.list_items() == []


def test_update_body_and_comment(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="Old\n\nBody"))

    provider.update_body(item.id, "New\n\nBody")
    assert provider.get_item(item.id).body == "New\n\nBody"

    provider.add_comment(item.id, "before", author="Axel")
    provider.update_comment(item.id, 0, "after", author="Horizon")
    comment = provider.get_item(item.id).metadata["comments"][0]
    assert comment["body"] == "after"
    assert comment["author"] == "Horizon"
    assert comment["edited_at"]


def test_rejected_item_is_not_accepted(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(
        InboxDraft(kind=InboxKind.ISSUE, body="x", labels=(REJECTED,))
    )
    assert not is_agent_ready(provider.get_item(item.id).labels)
