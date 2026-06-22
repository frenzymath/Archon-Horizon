"""The ``github`` inbox: a read/sync shadow of GitHub issues and PRs.

The roadmap treats GitHub as read-only from the dashboard's point of view.
This provider mirrors GitHub issues/PRs into a local shadow file via the
``gh`` CLI, so agents and the dashboard can read them offline. GitHub items
are NOT accepted by default — only an item that carries the ``archon:accept``
label (and not pending/rejected) is visible to agents, exactly like the local
inbox's acceptance gate.

Mutations are explicit, ``gh``-backed operations (label edits, comments) that
change GitHub directly; the next :meth:`sync` reflects them back into the
shadow. ``sync``/reads degrade gracefully when ``gh`` is missing: a failed
sync returns a :class:`SyncResult` carrying errors (never raises) and leaves
the cached shadow intact, so reads keep working offline.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from archon_horizon.core.clock import utc_now
from archon_horizon.core.inbox import (
    InboxFilter,
    InboxItem,
    InboxKind,
    InboxStatus,
    SyncResult,
    matches_filter,
)
from archon_horizon.core.labels import ARCHON_LABELS
from archon_horizon.store import serde
from archon_horizon.store.codec import Codec, YamlCodec

from .base import InboxProvider

Runner = Callable[[list[str]], str]

_JSON_FIELDS = "number,title,body,labels,state,updatedAt,comments"
_CLOSED_STATES = {"CLOSED", "MERGED"}


def _default_gh_runner(argv: list[str]) -> str:
    """Run ``gh <argv>`` and return stdout. Raises if ``gh`` is unavailable."""
    result = subprocess.run(["gh", *argv], capture_output=True, text=True, check=True)
    return result.stdout


class GithubInboxProvider(InboxProvider):
    capabilities = frozenset({"read", "sync", "label", "comment"})

    def __init__(
        self,
        repo: str,
        shadow_path: Path,
        *,
        name: str = "github",
        runner: Runner | None = None,
        codec: Codec | None = None,
    ) -> None:
        self.name = name
        self._repo = repo
        self._path = shadow_path
        self._runner = runner or _default_gh_runner
        self._codec = codec or YamlCodec()

    # ── shadow persistence ──────────────────────────────────────────

    def _load(self) -> dict[str, InboxItem]:
        if not self._path.exists():
            return {}
        data = self._codec.loads(self._path.read_text("utf-8")) or {}
        items = (serde.inbox_item_from_dict(d) for d in data.get("items", []))
        return {item.id: item for item in items}

    def _save(self, items: dict[str, InboxItem]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "items": [serde.to_jsonable(item) for item in items.values()]}
        self._path.write_text(self._codec.dumps(payload), "utf-8")

    # ── read (offline, from the shadow) ─────────────────────────────

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

    def _map(self, raw: dict, prefix: str, kind: InboxKind) -> InboxItem:
        number = raw["number"]
        labels = tuple(
            label["name"]
            for label in raw.get("labels", [])
            if label.get("name") in ARCHON_LABELS
        )
        state = (raw.get("state") or "").upper()
        status = InboxStatus.COMPLETED if state in _CLOSED_STATES else InboxStatus.OPEN
        body = f"{raw.get('title', '')}\n\n{raw.get('body') or ''}".strip()
        when = self._parse_dt(raw.get("updatedAt"))
        return InboxItem(
            id=f"gh-{prefix}-{number}",
            provider=self.name,
            kind=kind,
            body=body,
            labels=labels,
            status=status,
            source_ref=f"{prefix}:{number}",
            created_at=when,
            updated_at=when,
            metadata={"number": number, "comments": raw.get("comments", [])},
        )

    def sync(self) -> SyncResult:
        items: dict[str, InboxItem] = {}
        errors: list[str] = []
        for entity, prefix, kind in (
            ("issue", "issue", InboxKind.ISSUE),
            ("pr", "pr", InboxKind.REVIEW),
        ):
            try:
                for raw in self._fetch(entity):
                    item = self._map(raw, prefix, kind)
                    items[item.id] = item
            except Exception as exc:  # noqa: BLE001 — gh failures must never propagate
                errors.append(f"{entity}: {exc}")

        if errors:
            # Preserve the cached shadow so offline reads keep working.
            return SyncResult(provider=self.name, errors=tuple(errors))

        self._save(items)
        return SyncResult(provider=self.name, imported=len(items))

    # ── explicit gh-backed mutations (change GitHub, not the shadow) ─

    def _entity_and_number(self, item_id: str) -> tuple[str, str]:
        prefix, number = (self.get_item(item_id).source_ref or "").split(":", 1)
        return ("issue" if prefix == "issue" else "pr"), number

    def update_labels(self, item_id: str, labels: list[str]) -> None:
        entity, number = self._entity_and_number(item_id)
        self._runner([entity, "edit", number, "--repo", self._repo, "--add-label", ",".join(labels)])

    def add_comment(self, item_id: str, body: str) -> None:
        entity, number = self._entity_and_number(item_id)
        self._runner([entity, "comment", number, "--repo", self._repo, "--body", body])
