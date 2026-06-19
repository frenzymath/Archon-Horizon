"""Filesystem local inbox: CRUD, persistence, labels, and accepted filtering."""

from __future__ import annotations

from pathlib import Path

from archon_horizon.core.inbox import InboxDraft, InboxFilter, InboxKind, InboxScope, InboxStatus
from archon_horizon.core.labels import ARCHON_PENDING, ARCHON_REJECTED, is_accepted
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider


def _provider(tmp_path: Path) -> FilesystemInboxProvider:
    return FilesystemInboxProvider(tmp_path / ".archon-horizon" / "inboxes" / "local.yaml")


def test_create_defaults_to_accepted_and_persists(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    created = provider.create_item(
        InboxDraft(kind=InboxKind.HINT, body="Try the affine case first.")
    )
    assert created.id == "I-0001"
    assert is_accepted(created.labels)

    # A fresh provider over the same file sees the persisted item.
    reloaded = FilesystemInboxProvider(provider._path).get_item("I-0001")  # noqa: SLF001
    assert reloaded.body == "Try the affine case first."


def test_ids_increment_and_filter_by_kind_and_project(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider.create_item(InboxDraft(kind=InboxKind.HINT, body="h", scope=InboxScope(project="a")))
    second = provider.create_item(
        InboxDraft(kind=InboxKind.PROPOSAL, body="p", scope=InboxScope(project="b"))
    )
    assert second.id == "I-0002"

    proposals = provider.list_items(InboxFilter(kinds=(InboxKind.PROPOSAL,)))
    assert [i.id for i in proposals] == ["I-0002"]
    assert provider.list_items(InboxFilter(project="a"))[0].id == "I-0001"


def test_relabel_pending_makes_it_unaccepted(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(InboxDraft(kind=InboxKind.ISSUE, body="x"))
    provider.update_labels(item.id, [ARCHON_PENDING])
    assert not is_accepted(provider.get_item(item.id).labels)


def test_status_comment_and_delete(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="x"))

    provider.update_status(item.id, InboxStatus.COMPLETED)
    assert provider.get_item(item.id).status is InboxStatus.COMPLETED

    provider.add_comment(item.id, "done in T-0007")
    assert provider.get_item(item.id).metadata["comments"][0]["body"] == "done in T-0007"

    provider.delete_item(item.id)
    assert provider.list_items() == []


def test_rejected_item_is_not_accepted(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    item = provider.create_item(
        InboxDraft(kind=InboxKind.ISSUE, body="x", labels=(ARCHON_REJECTED,))
    )
    assert not is_accepted(provider.get_item(item.id).labels)
