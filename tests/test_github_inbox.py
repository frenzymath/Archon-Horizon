"""GitHub shadow inbox: sync mapping, offline reads, acceptance gate, graceful failure."""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.core.inbox import InboxKind, InboxStatus
from archon_horizon.core.labels import is_accepted
from archon_horizon.inboxes.github import GithubInboxProvider

ISSUES_JSON = json.dumps(
    [
        {
            "number": 42,
            "title": "Affine cover bug",
            "body": "The cover lemma is wrong.",
            "state": "OPEN",
            "updatedAt": "2024-01-02T03:04:05Z",
            "labels": [{"name": "archon:accept"}, {"name": "bug"}],
            "comments": [],
        },
        {
            "number": 7,
            "title": "Question about Spec",
            "body": "How is Spec used?",
            "state": "OPEN",
            "updatedAt": "2024-01-03T00:00:00Z",
            "labels": [{"name": "question"}],
            "comments": [],
        },
    ]
)


def _runner(argv: list[str]) -> str:
    if argv[:2] == ["issue", "list"]:
        return ISSUES_JSON
    if argv[:2] == ["pr", "list"]:
        return "[]"
    raise AssertionError(f"unexpected gh argv: {argv}")


def _boom(argv: list[str]) -> str:
    raise FileNotFoundError("gh: command not found")


def test_sync_imports_maps_and_persists(tmp_path: Path) -> None:
    shadow = tmp_path / ".archon-horizon" / "inboxes" / "github-shadow.yaml"
    provider = GithubInboxProvider("owner/repo", shadow, runner=_runner)

    result = provider.sync()

    assert result.imported == 2
    assert result.errors == ()
    assert shadow.exists()

    item = provider.get_item("gh-issue-42")
    assert item.kind is InboxKind.ISSUE
    assert item.source_ref == "issue:42"
    assert item.status is InboxStatus.OPEN
    assert "Affine cover bug" in item.body


def test_reads_are_offline_and_acceptance_is_label_gated(tmp_path: Path) -> None:
    shadow = tmp_path / "github-shadow.yaml"
    GithubInboxProvider("owner/repo", shadow, runner=_runner).sync()

    # Fresh instance whose runner always raises: reads must still work from cache.
    offline = GithubInboxProvider("owner/repo", shadow, runner=_boom)
    by_id = {item.id: item for item in offline.list_items()}

    assert set(by_id) == {"gh-issue-42", "gh-issue-7"}
    assert is_accepted(by_id["gh-issue-42"].labels)  # carries archon:accept
    assert not is_accepted(by_id["gh-issue-7"].labels)  # no archon:* label


def test_sync_failure_returns_errors_without_crashing(tmp_path: Path) -> None:
    shadow = tmp_path / "github-shadow.yaml"
    provider = GithubInboxProvider("owner/repo", shadow, runner=_boom)

    result = provider.sync()

    assert result.imported == 0
    assert result.errors  # populated, not raised
    assert not shadow.exists()  # cache left intact (here: never written)
