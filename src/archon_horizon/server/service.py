"""Live workspace service — the data layer behind the server.

Pure-ish application logic with no socket/HTTP concerns, so it is testable
directly. Unlike the static dashboard, this exposes inbox editing: the
roadmap permits the *live local* dashboard to edit the local inbox (the
static page never persists edits).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from archon_horizon.blueprint.workspace import workspace_dags
from archon_horizon.config.loader import build_stores, build_workspace, load_config
from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxStatus
from archon_horizon.core.labels import ARCHON_ACCEPT, ARCHON_PENDING, ARCHON_REJECTED
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.inboxes.github import GithubInboxProvider
from archon_horizon.runlog import SessionLog
from archon_horizon.store import serde
from archon_horizon.transcript.sink import read_transcript


class WorkspaceService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = load_config(root)
        self.workspace = build_workspace(self.cfg, root)
        self.stores = build_stores(self.workspace)
        self.local = FilesystemInboxProvider(self.workspace.state_path / "inboxes" / "local.yaml")
        self.github = (
            GithubInboxProvider(
                self.cfg.github.repo, self.workspace.state_path / "inboxes" / "github-shadow.yaml"
            )
            if self.cfg.github.enabled and self.cfg.github.repo
            else None
        )

    # ── read ────────────────────────────────────────────────────────

    def state(self, *, events_tail: int = 50) -> dict[str, Any]:
        return {
            "workspace": self.workspace.name,
            "roadmap": serde.to_jsonable(self.stores.roadmap.load()),
            "tasks": [serde.to_jsonable(t) for t in self.stores.tasks.list()],
            "proposals": [serde.to_jsonable(p) for p in self.stores.proposals.list()],
            "local_inbox": [serde.to_jsonable(i) for i in self.local.list_items()],
            "github_inbox": (
                [serde.to_jsonable(i) for i in self.github.list_items()] if self.github else []
            ),
            "memory": self.stores.memory.load(),
            "reports": self.stores.reports.list(),
            "blueprints": workspace_dags(self.workspace),
            "events": [serde.to_jsonable(e) for e in self.stores.events.read_all()[-events_tail:]],
        }

    def transcripts(self) -> list[dict[str, Any]]:
        """Flatten every run's ordered sessions (and nested subagents)."""
        found: list[dict[str, Any]] = []
        for run_id in self.stores.run_logs.ids():
            for session in self.stores.run_logs.get(run_id).sessions():
                self._collect_session(run_id, session, "", found)
        return found

    def _collect_session(
        self, run_id: str, session: SessionLog, parent: str, found: list[dict[str, Any]]
    ) -> None:
        if session.transcript_path.exists():
            found.append({
                "run": run_id,
                "session": session.name,
                "parent": parent,
                "ref": session.transcript_path.relative_to(self.root).as_posix(),
                "meta": session.read_meta(),
            })
        for child in session.subsessions():
            self._collect_session(run_id, child, session.name, found)

    def render_html(self, *, live: bool = True) -> str:
        from archon_horizon.render.dashboard import render_dashboard

        return render_dashboard(
            workspace_name=self.workspace.name,
            roadmap=self.stores.roadmap.load(),
            local_items=self.local.list_items(),
            github_items=self.github.list_items() if self.github else (),
            memory=self.stores.memory.load(),
            reports=self.stores.reports.list(),
            live=live,
        )

    def transcript(self, ref: str) -> list[dict[str, Any]]:
        path = (self.root / ref).resolve()
        if self.root.resolve() not in path.parents:  # contain path traversal
            raise ValueError("transcript ref escapes the workspace")
        return [serde.to_jsonable(e) for e in read_transcript(path)]

    # ── endpoint registry (shared by live server + static export) ────

    def endpoints(self) -> list[str]:
        """Every GET path the dashboard reads. The live server and the static
        exporter both go through this, so they can never drift."""
        eps = ["/api/state", "/api/transcripts"]
        eps += [f"/api/transcript?ref={t['ref']}" for t in self.transcripts()]
        return eps

    def serve_endpoint(self, path: str) -> Any:
        """Resolve one GET path (with query) to its JSON payload."""
        parsed = urlparse(path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/state":
            return self.state()
        if parsed.path == "/api/transcripts":
            return self.transcripts()
        if parsed.path == "/api/transcript":
            return self.transcript((query.get("ref") or [""])[0])
        raise KeyError(path)

    # ── write (local inbox only) ─────────────────────────────────────

    def edit_inbox(self, action: str, **kw: Any) -> dict[str, Any]:
        if action == "add":
            labels = (ARCHON_PENDING,) if kw.get("pending") else (ARCHON_ACCEPT,)
            item = self.local.create_item(
                InboxDraft(kind=InboxKind(kw.get("kind", "hint")), body=kw["body"], labels=labels)
            )
            return {"created": item.id}
        item_id = kw["id"]
        if action == "label":
            self.local.update_labels(item_id, list(kw["labels"]))
        elif action == "complete":
            self.local.update_status(item_id, InboxStatus.COMPLETED)
        elif action == "reject":
            self.local.update_labels(item_id, [ARCHON_REJECTED])
        elif action == "archive":
            self.local.update_status(item_id, InboxStatus.ARCHIVED)
        elif action == "delete":
            self.local.delete_item(item_id)
        else:
            raise ValueError(f"unknown inbox action {action!r}")
        return {"ok": True, "id": item_id}
