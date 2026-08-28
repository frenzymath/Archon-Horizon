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


def test_filter_by_audience_and_query(tmp_path: Path) -> None:
    provider = _provider(tmp_path)
    provider.create_item(
        InboxDraft(
            kind=InboxKind.HINT,
            body="Horizon merge\n\nUse the pullback route.",
            audience="horizon",
            scope=InboxScope(projects=("p",)),
        )
    )
    provider.create_item(
        InboxDraft(
            kind=InboxKind.INFO,
            body="Human notice\n\nThe merge was reported.",
            audience="human",
        )
    )

    provider.add_comment("I-0001", "The existing thread already mentions tensoring.")

    filtered = provider.list_items(InboxFilter(audience="horizon", query="tensoring", project="p"))

    assert [item.id for item in filtered] == ["I-0001"]


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


def test_status_filtered_list_skips_other_statuses_and_history(tmp_path: Path) -> None:
    """Open-only list must not hydrate archived history or return closed rows."""
    provider = _provider(tmp_path)
    open_item = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="keep open"))
    closed = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="close me"))
    archived = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="archive me"))
    provider.update_status(closed.id, InboxStatus.CLOSED)
    provider.update_status(archived.id, InboxStatus.ARCHIVED)
    provider.add_comment(open_item.id, "live reply")
    # History on the archived row must not appear when listing open items only.
    provider._record(archived.id, "human", "status", before="open", after="archived")  # noqa: SLF001

    opened = provider.list_items(InboxFilter(status=InboxStatus.OPEN))
    assert [item.id for item in opened] == [open_item.id]
    assert opened[0].metadata.get("comments")
    assert "history" not in opened[0].metadata

    all_items = provider.list_items()
    by_id = {item.id: item for item in all_items}
    assert set(by_id) == {open_item.id, closed.id, archived.id}
    assert by_id[archived.id].metadata.get("history")

    # Single-item fetch still hydrates comments + history.
    full = provider.get_item(archived.id)
    assert full.metadata.get("history")


def test_create_does_not_require_parsing_existing_bodies(tmp_path: Path) -> None:
    """Id allocation scans filenames only — a corrupt sibling must not block create."""
    provider = _provider(tmp_path)
    first = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="ok"))
    # Unparseable file that is not touched by create's id scan beyond its stem.
    bad = provider._items_dir / "I-9999.yaml"  # noqa: SLF001
    bad.write_text("not: valid: yaml: [", "utf-8")
    second = provider.create_item(InboxDraft(kind=InboxKind.HINT, body="still ok"))
    assert second.id == "I-10000"
    assert provider.get_item(first.id).body == "ok"
