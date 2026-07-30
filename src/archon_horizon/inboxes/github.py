"""The ``github`` inbox: a synced file mirror of GitHub issues and PRs.

GitHub is the source of truth for this provider. Reads use the local mirror for
offline/dashboard access, while mutations call ``gh`` first and then refresh the
mirror. The mirror uses the same provider shape as the local inbox:
``items/*.yaml`` plus ``comments/<item-id>/*.md``.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import (
    InboxFilter,
    InboxItem,
    InboxKind,
    InboxStatus,
    SyncResult,
    matches_filter,
)
from archon_horizon.core.labels import AGENT_READY, NOT_READY, REJECTED, TRIAGE_LABELS
from archon_horizon.store import serde
from archon_horizon.store.codec import Codec, YamlCodec

from .base import InboxProvider
from .sharded import read_comments, without_comments, write_comment

Runner = Callable[[list[str]], str]

_JSON_FIELDS = "number,title,body,labels,state,createdAt,updatedAt,comments,url,author"
_CLOSED_STATES = {"CLOSED", "MERGED"}
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _default_gh_runner(argv: list[str]) -> str:
    """Run ``gh <argv>`` and return stdout. Raises if ``gh`` is unavailable."""
    result = subprocess.run(["gh", *argv], capture_output=True, text=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        command = "gh " + " ".join(argv)
        raise RuntimeError(f"{command} failed: {detail or f'exit {result.returncode}'}")
    return result.stdout


def _safe_id(value: object) -> str:
    return _SAFE_ID_RE.sub("-", str(value)).strip("-") or "unknown"


class GithubInboxProvider(InboxProvider):
    capabilities = frozenset({"read", "sync", "label", "comment", "status"})

    def __init__(
        self,
        repo: str,
        mirror_root: Path,
        *,
        name: str = "github",
        import_policy: str = "all",
        runner: Runner | None = None,
        codec: Codec | None = None,
    ) -> None:
        self.name = name
        self._repo = repo
        self._path = mirror_root
        self._import_policy = import_policy
        self._runner = runner or _default_gh_runner
        self._codec = codec or YamlCodec()

    # ── mirror persistence ──────────────────────────────────────────

    @property
    def _items_dir(self) -> Path:
        return self._path / "items"

    @property
    def _comments_dir(self) -> Path:
        return self._path / "comments"

    def _item_path(self, item_id: str) -> Path:
        return self._items_dir / f"{item_id}.{self._codec.extension}"

    def _comment_dir(self, item_id: str) -> Path:
        return self._comments_dir / item_id

    def _load(self) -> dict[str, InboxItem]:
        if not self._items_dir.exists():
            return {}
        items: dict[str, InboxItem] = {}
        for path in sorted(self._items_dir.glob(f"*.{self._codec.extension}")):
            item = serde.inbox_item_from_dict(self._codec.loads(path.read_text("utf-8")))
            comments = read_comments(self._comment_dir(item.id))
            if comments:
                item = dataclasses.replace(item, metadata={**item.metadata, "comments": comments})
            items[item.id] = item
        return items

    def _save(self, items: dict[str, InboxItem]) -> None:
        self._items_dir.mkdir(parents=True, exist_ok=True)
        wanted = set(items)
        for item in items.values():
            self._save_item(item)
            self._save_comments(item)
        for path in self._items_dir.glob(f"*.{self._codec.extension}"):
            if path.stem not in wanted:
                path.unlink()
                shutil.rmtree(self._comment_dir(path.stem), ignore_errors=True)
        if self._comments_dir.exists():
            for directory in self._comments_dir.iterdir():
                if directory.is_dir() and directory.name not in wanted:
                    shutil.rmtree(directory, ignore_errors=True)

    def _save_item(self, item: InboxItem) -> None:
        self._items_dir.mkdir(parents=True, exist_ok=True)
        stored = dataclasses.replace(item, metadata=without_comments(item.metadata))
        self._item_path(item.id).write_text(self._codec.dumps(serde.to_jsonable(stored)), "utf-8")

    def _save_comments(self, item: InboxItem) -> None:
        comments = [dict(comment) for comment in item.metadata.get("comments", [])]
        directory = self._comment_dir(item.id)
        if directory.exists():
            shutil.rmtree(directory)
        for index, comment in enumerate(comments, start=1):
            comment_id = str(comment.get("id") or f"ghc-{index:04d}")
            comment["id"] = comment_id
            comment.setdefault("item", item.id)
            comment.setdefault("provider", self.name)
            comment.setdefault("repo", self._repo)
            write_comment(directory / f"{comment_id}.md", comment)

    # ── read (offline, from the mirror) ─────────────────────────────

    def list_items(self, filters: InboxFilter | None = None) -> list[InboxItem]:
        return [item for item in self._load().values() if matches_filter(item, filters)]

    def get_item(self, item_id: str) -> InboxItem:
        return self._load()[item_id]

    # ── sync (import from GitHub) ───────────────────────────────────

    def _fetch(self, entity: str) -> list[dict]:
        argv = [entity, "list", "--repo", self._repo, "--state", "all", "--json", _JSON_FIELDS]
        return json.loads(self._runner(argv) or "[]")

    @staticmethod
    def _parse_dt(value: str | None) -> datetime:
        if not value:
            return utc_now()
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return utc_now()

    def _map_comment(self, raw: dict[str, Any], item_id: str, index: int) -> dict[str, Any]:
        raw_id = raw.get("id") or raw.get("databaseId") or raw.get("url") or index
        author_raw = raw.get("author")
        author = author_raw.get("login", "") if isinstance(author_raw, dict) else (author_raw or "")
        created = raw.get("createdAt") or raw.get("created_at") or raw.get("at")
        updated = raw.get("updatedAt") or raw.get("updated_at") or created
        return {
            "id": f"ghc-{_safe_id(raw_id)}",
            "provider": self.name,
            "repo": self._repo,
            "item": item_id,
            "remote_id": str(raw_id),
            "remote_url": raw.get("url"),
            "author": f"github:{author}" if author else "github",
            "at": self._parse_dt(created).isoformat() if created else utc_now().isoformat(),
            "updated_at": self._parse_dt(updated).isoformat() if updated else None,
            "body": raw.get("body") or "",
        }

    def _map(self, raw: dict, prefix: str, kind: InboxKind) -> InboxItem:
        number = raw["number"]
        labels = tuple(
            label["name"]
            for label in raw.get("labels", [])
            if label.get("name") in TRIAGE_LABELS
        )
        state = (raw.get("state") or "").upper()
        status = InboxStatus.CLOSED if state in _CLOSED_STATES else InboxStatus.OPEN
        body = f"{raw.get('title', '')}\n\n{raw.get('body') or ''}".strip()
        created = self._parse_dt(raw.get("createdAt"))
        updated = self._parse_dt(raw.get("updatedAt"))
        author_raw = raw.get("author")
        author = author_raw.get("login", "") if isinstance(author_raw, dict) else (author_raw or "")
        item_id = f"{prefix}-{number}"
        comments = [
            self._map_comment(comment, item_id, index)
            for index, comment in enumerate(raw.get("comments", []), start=1)
        ]
        return InboxItem(
            id=item_id,
            provider=self.name,
            kind=kind,
            body=body,
            labels=labels,
            status=status,
            author=f"github:{author}" if author else "github",
            source_ref=f"{prefix}:{number}",
            created_at=created,
            updated_at=updated,
            metadata={"number": number, "remote_type": prefix, "url": raw.get("url"), "comments": comments},
        )

    def _should_import(self, item: InboxItem) -> bool:
        policy = self._import_policy
        if policy in {"", "all"}:
            return True
        if policy == "labeled-only":
            return bool(set(item.labels) & TRIAGE_LABELS)
        if policy == "accepted-only":
            return AGENT_READY in item.labels
        return True

    def sync(self) -> SyncResult:
        before = self._load()
        items: dict[str, InboxItem] = {}
        errors: list[str] = []
        for entity, prefix, kind in (
            ("issue", "issue", InboxKind.ISSUE),
            ("pr", "pr", InboxKind.ISSUE),
        ):
            try:
                for raw in self._fetch(entity):
                    item = self._map(raw, prefix, kind)
                    if self._should_import(item):
                        items[item.id] = item
            except Exception as exc:  # noqa: BLE001 — gh failures must never propagate
                errors.append(f"{entity}: {exc}")

        if errors:
            # Preserve the cached mirror so offline reads keep working.
            return SyncResult(provider=self.name, errors=tuple(errors))

        self._save(items)
        imported = len(set(items) - set(before))
        updated = sum(1 for key, item in items.items() if key in before and item != before[key])
        skipped = max(0, len(before) - len(items))
        return SyncResult(provider=self.name, imported=imported, updated=updated, skipped=skipped)

    # ── explicit gh-backed mutations (change GitHub, then refresh mirror) ─

    def _entity_and_number(self, item_id: str) -> tuple[str, str]:
        prefix, number = (self.get_item(item_id).source_ref or "").split(":", 1)
        return ("issue" if prefix == "issue" else "pr"), number

    def update_labels(self, item_id: str, labels: list[str], actor: str | None = None) -> None:
        entity, number = self._entity_and_number(item_id)
        current = set(self.get_item(item_id).labels)
        desired = set(labels)
        to_add = sorted(desired - current)
        to_remove = sorted((current & TRIAGE_LABELS) - desired)
        for label in to_add:
            if label in TRIAGE_LABELS:
                self._ensure_label(label)
        argv = [entity, "edit", number, "--repo", self._repo]
        if to_add:
            argv += ["--add-label", ",".join(to_add)]
        if to_remove:
            argv += ["--remove-label", ",".join(to_remove)]
        if len(argv) > 5:
            self._runner(argv)
            self.sync()

    def _ensure_label(self, label: str) -> None:
        colors = {
            AGENT_READY: "0E8A16",
            NOT_READY: "FBCA04",
            REJECTED: "B60205",
        }
        descriptions = {
            AGENT_READY: "Released to the Horizon agents at sync boundaries",
            NOT_READY: "Held for human review before agent use",
            REJECTED: "Rejected for Horizon agent use",
        }
        try:
            self._runner([
                "label",
                "create",
                label,
                "--repo",
                self._repo,
                "--color",
                colors.get(label, "C5DEF5"),
                "--description",
                descriptions.get(label, "Archon Horizon inbox label"),
            ])
        except Exception as exc:  # noqa: BLE001
            # Existing labels and permission-denied cases are both resolved by
            # the following issue/pr edit. If the label still cannot be used,
            # that edit raises the actionable gh error for the dashboard.
            if "already exists" not in str(exc).lower():
                return

    def update_status(self, item_id: str, status: InboxStatus, actor: str | None = None) -> None:
        entity, number = self._entity_and_number(item_id)
        verb = "close" if status is InboxStatus.CLOSED else "reopen"
        self._runner([entity, verb, number, "--repo", self._repo])
        self.sync()

    def add_comment(
        self,
        item_id: str,
        body: str,
        author: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        entity, number = self._entity_and_number(item_id)
        self._runner([entity, "comment", number, "--repo", self._repo, "--body", body])
        self.sync()
