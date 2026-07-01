"""Live workspace service — the data layer behind the server.

Pure-ish application logic with no socket/HTTP concerns, so it is testable
directly. Unlike the static dashboard, this exposes inbox editing: the
roadmap permits the *live local* dashboard to edit the local inbox (the
static page never persists edits).
"""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from datetime import datetime, timezone

from archon_horizon.log import log

from archon_horizon.blueprint.chapters import project_chapters
from archon_horizon.blueprint.workspace import published_dags, workspace_dags
from archon_horizon.config.loader import build_stores, build_workspace, load_config
from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxStatus
from archon_horizon.core.labels import AGENT_READY, NOT_READY, REJECTED
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapKind, RoadmapStatus
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.inboxes.github import GithubInboxProvider
from archon_horizon.runlog import RunLog, SessionLog
from archon_horizon.store import serde
from archon_horizon.transcript.parsers import observed_model
from archon_horizon.transcript.sink import read_transcript
from archon_horizon.transcript.subagents import materialize_subagent_sessions
from archon_horizon.orchestration.locks import live_run_lock
from archon_horizon.server.git_api import get_git_log, get_git_diff
from archon_horizon.server.source_api import list_lean_files, read_lean_file


def _flatten_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for session in sessions:
        out.append(session)
        out.extend(_flatten_sessions(session.get("children", [])))
    return out


# A "running" session is only genuinely active if its process is alive or it
# emitted activity recently. This window covers concurrent runs (which the run
# lock doesn't track) and a quiet-but-alive engine (e.g. a long Lean build);
# past it, a still-"running" session with a dead process reads as interrupted.
_ACTIVE_WINDOW_S = 300.0


def _is_recent(iso: str, window_s: float = _ACTIVE_WINDOW_S) -> bool:
    if not iso:
        return False
    try:
        ts = datetime.fromisoformat(iso)
    except ValueError:
        return False
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (now - ts).total_seconds() <= window_s


def _usage_value(usage: Any, key: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _sum_usage(usages: Any) -> dict[str, Any]:
    total = {
        "tokens_in": 0,
        "tokens_out": 0,
        "cached_tokens_in": 0,
        "reasoning_tokens_out": 0,
        "cost_usd": 0.0,
    }
    has_cost = False
    for usage in usages:
        if not usage:
            continue
        total["tokens_in"] += int(_usage_value(usage, "tokens_in") or 0)
        total["tokens_out"] += int(_usage_value(usage, "tokens_out") or 0)
        total["cached_tokens_in"] += int(_usage_value(usage, "cached_tokens_in") or 0)
        total["reasoning_tokens_out"] += int(_usage_value(usage, "reasoning_tokens_out") or 0)
        cost = _usage_value(usage, "cost_usd")
        if cost is not None:
            total["cost_usd"] += float(cost)
            has_cost = True
    if not has_cost:
        total["cost_usd"] = None
    return total


class WorkspaceService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = load_config(root)
        self.workspace = build_workspace(self.cfg, root)
        self.stores = build_stores(self.workspace)
        self._search_index = None
        self._search_lock = threading.Lock()
        self.local = FilesystemInboxProvider(self.workspace.state_path / "inbox" / "local")
        self.github = (
            GithubInboxProvider(
                self.cfg.github.repo,
                self.workspace.state_path / "inbox" / "github",
                import_policy=self.cfg.github.import_policy,
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
            "runs": self._runs_state(),
            "local_inbox": [serde.to_jsonable(i) for i in self.local.list_items()],
            "github_inbox": (
                [serde.to_jsonable(i) for i in self.github.list_items()] if self.github else []
            ),
            "inbox_providers": self._inbox_provider_state(),
            "memory": self.stores.memory.load(),
            "reports": self.stores.reports.list(),
            "blueprints": published_dags(self.workspace),
            "projects": self._discover_projects(),
            "libraries": self._search_libraries(),
            "harnesses": self._harness_state(),
            "events": [serde.to_jsonable(e) for e in self.stores.events.read_all()[-events_tail:]],
        }

    def _discover_projects(self) -> list[str]:
        projs = list(self.workspace.projects.keys())
        if not projs:
            for p in sorted(self.workspace.root.iterdir(), key=lambda x: x.name):
                if p.is_dir() and not p.name.startswith(".") and p.name != "src":
                    projs.append(p.name)
        return sorted(dict.fromkeys(projs))

    def _project_path(self, name: str) -> Path:
        """Resolve a known project name to its absolute path (no traversal)."""
        if name in self.workspace.projects:
            return self.workspace.project_path(name)
        candidate = (self.workspace.root / name).resolve()
        if self.workspace.root.resolve() in candidate.parents and name in self._discover_projects():
            return candidate
        raise KeyError(f"unknown project {name!r}")

    def projects_summary(self) -> dict[str, Any]:
        """Per-project metrics for the overview: Lean LOC/sorries + blueprint size."""
        dags = workspace_dags(self.workspace)
        projects: list[dict[str, Any]] = []
        for name in self._discover_projects():
            try:
                files = list_lean_files(self._project_path(name))
            except KeyError:
                files = []
            dag = dags.get(name) or {}
            nodes = dag.get("nodes", [])
            projects.append({
                "name": name,
                "depends_on": list(self.workspace.projects.get(name).depends_on) if name in self.workspace.projects else [],
                "lean_files": len(files),
                "loc": sum(f["loc"] for f in files),
                "loc_code": sum(f["loc_code"] for f in files),
                "sorries": sum(f["sorries"] for f in files),
                "blueprint_nodes": len(nodes),
                "blueprint_leanok": sum(1 for n in nodes if n.get("leanok")),
            })
        totals = {
            "lean_files": sum(p["lean_files"] for p in projects),
            "loc": sum(p["loc"] for p in projects),
            "loc_code": sum(p["loc_code"] for p in projects),
            "sorries": sum(p["sorries"] for p in projects),
            "blueprint_nodes": sum(p["blueprint_nodes"] for p in projects),
            "blueprint_leanok": sum(p["blueprint_leanok"] for p in projects),
        }
        return {"projects": projects, "totals": totals}

    def _harness_state(self) -> dict[str, Any]:
        return {
            name: {
                "name": cfg.name,
                "kind": cfg.kind,
                "command": cfg.command,
                "args": list(cfg.args),
                "model": cfg.model,
                "options": dict(cfg.options),
            }
            for name, cfg in self.cfg.harnesses.items()
        }

    def _runs_state(self) -> list[dict[str, Any]]:
        records = {run.id: serde.to_jsonable(run) for run in self.stores.runs.list()}
        run_events: dict[str, list[dict[str, Any]]] = {}
        for event in self.stores.events.read_all():
            run_id = event.data.get("run_id")
            if isinstance(run_id, str) and run_id:
                run_events.setdefault(run_id, []).append(serde.to_jsonable(event))
        holder = live_run_lock(self.workspace.state_path / "run.lock")
        live_run_id = str(holder.get("run_id")) if holder and holder.get("run_id") else None
        return [
            self._run_state(
                self.stores.run_logs.get(run_id),
                records.get(run_id, {}),
                run_events.get(run_id, []),
                live_run_id,
            )
            for run_id in reversed(self.stores.run_logs.ids())
        ]

    def _run_state(
        self,
        run: RunLog,
        record: dict[str, Any],
        events: list[dict[str, Any]],
        live_run_id: str | None = None,
    ) -> dict[str, Any]:
        sessions = [self._session_state(run.id, session, "") for session in run.sessions()]
        flat = _flatten_sessions(sessions)
        failed = any(session["status"] == "failed" for session in flat)
        running = any(session["status"] == "running" for session in flat)
        # A terminal `run.stopped` event means the orchestrator is gone, so no
        # session is still live regardless of timing. Absent that (a hard crash),
        # a run with "running" sessions is only truly active when its process
        # holds the run lock or some session emitted activity recently (the
        # latter covers concurrent runs the single-owner lock can't see).
        run_ended = any(event.get("type") == "run.stopped" for event in events)
        active = running and not run_ended and (
            run.id == live_run_id
            or any(_is_recent(session.get("last_at", "")) for session in flat)
        )
        interrupted = False
        if running and not active:
            for session in flat:
                if session["status"] == "running":
                    session["status"] = "interrupted"
            running = False
            interrupted = True
        completed = bool(flat) and not failed and not running and not interrupted
        rounds_done = {
            session.get("meta", {}).get("round")
            for session in flat
            if isinstance(session.get("meta", {}).get("round"), int)
        }
        usage = _sum_usage(session.get("usage") for session in flat)
        return {
            **record,
            "id": run.id,
            "status": (
                "failed" if failed
                else "running" if running
                else "interrupted" if interrupted
                else "completed" if completed
                else "created"
            ),
            "rounds_completed": len(rounds_done),
            "session_count": len(flat),
            "usage": usage,
            "stop": next((event["data"] for event in reversed(events) if event.get("type") == "run.stopped"), None),
            "sessions": sessions,
        }

    def _session_state(self, run_id: str, session: SessionLog, parent: str) -> dict[str, Any]:
        materialize_subagent_sessions(session.path)
        events = read_transcript(session.transcript_path) if session.transcript_path.exists() else []
        end = next((event for event in reversed(events) if event.kind == "session_end"), None)
        usage = _sum_usage(event.usage for event in events if event.usage is not None)
        meta = session.read_meta()
        status = "created"
        if end is not None:
            status = "completed" if end.data.get("ok", True) else "failed"
        elif "ok" in meta:
            status = "completed" if meta.get("ok") else "failed"
        elif isinstance(meta.get("data"), dict) and "ok" in meta["data"]:
            status = "completed" if meta["data"].get("ok") else "failed"
        elif any(event.kind == "error" for event in events):
            status = "failed"
        elif events:
            status = "running"
        elif meta:
            status = "completed"
        ref = session.transcript_path.relative_to(self.root).as_posix() if session.transcript_path.exists() else ""
        children = [self._session_state(run_id, child, session.name) for child in session.subsessions()]
        return {
            "run": run_id,
            "session": session.name,
            "parent": parent,
            "ref": ref,
            "meta": meta,
            "status": status,
            "model": observed_model(events) or meta.get("model"),
            "started_at": events[0].at.isoformat() if events else "",
            "ended_at": end.at.isoformat() if end else "",
            "last_at": events[-1].at.isoformat() if events else "",
            "usage": usage,
            "children": children,
        }

    def _inbox_provider_state(self) -> dict[str, Any]:
        return {
            "local": {
                "enabled": True,
                "repo": None,
                "capabilities": sorted(self.local.capabilities),
            },
            "github": {
                "enabled": self.github is not None,
                "repo": self.cfg.github.repo if self.github else None,
                "capabilities": sorted(self.github.capabilities) if self.github else [],
            },
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
        materialize_subagent_sessions(session.path)
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

    def report(self, ref: str) -> dict[str, Any]:
        """The session's ``report.md`` (the agent's prose summary), sitting next
        to its transcript ref. Empty markdown when the session hasn't reported."""
        path = (self.root / ref).resolve()
        if self.root.resolve() not in path.parents:  # contain path traversal
            raise ValueError("report ref escapes the workspace")
        report_path = path.parent / "report.md"
        return {"markdown": report_path.read_text("utf-8") if report_path.is_file() else ""}

    # ── search (Lean declarations + blueprint statements) ────────────

    def _search_libraries(self) -> list[str]:
        """The libraries the search index covers: every project plus each
        configured external library (e.g. ``mathlib``) — the facets to filter by."""
        names = list(self._discover_projects())
        names += [lib.name for lib in self.cfg.external_libraries if getattr(lib, "name", "")]
        return sorted(dict.fromkeys(n for n in names if n))

    def _get_search_index(self):
        """The Lean declaration index, built once per server and reused — building
        the BM25 over a large library (e.g. all of Mathlib) is the slow part, so a
        fresh index per query would make every search take seconds. A background
        pre-warm (see ``warm_search_index``) builds it before the first query."""
        if self._search_index is None:
            with self._search_lock:
                if self._search_index is None:
                    from archon_horizon.search.workspace import load_or_build_index

                    index = load_or_build_index(self.root, self.cfg)
                    index.bm25  # build the (slow) inverted index now, under the lock
                    self._search_index = index
        return self._search_index

    def warm_search_index(self) -> None:
        """Best-effort pre-build of the search index (call in a background thread
        on server start so the first user search doesn't pay the build cost)."""
        try:
            self._get_search_index()
        except Exception as exc:  # warming must never crash the server
            log.warn(f"search index warm-up failed: {exc}")

    def _blueprint_nodes(self, project: str | None) -> tuple[list[tuple[str, dict]], dict[str, tuple[str, dict]]]:
        """All blueprint nodes as ``(project, node)`` plus a ``lean-name -> (project,
        node)`` map, so a Lean hit can carry its informal LaTeX statement. Falls
        back to parsing the blueprint sources when the published cache is empty,
        so search still pairs Lean with LaTeX before any DAG has been published."""
        dags = published_dags(self.workspace)
        if not any(d.get("nodes") for d in dags.values()):
            dags = workspace_dags(self.workspace)
        nodes: list[tuple[str, dict]] = []
        by_lean: dict[str, tuple[str, dict]] = {}
        for proj, dag in dags.items():
            if project and proj != project:
                continue
            for node in dag.get("nodes", []):
                nodes.append((proj, node))
                lean = node.get("lean")
                if lean:
                    by_lean[lean] = (proj, node)
        return nodes, by_lean

    @staticmethod
    def _blueprint_payload(entry: tuple[str, dict] | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        proj, node = entry
        return {
            "project": proj,
            "id": node.get("id"),
            "kind": node.get("kind"),
            "title": node.get("title"),
            "statement": node.get("statement"),
            "leanok": node.get("leanok", False),
        }

    def _lean_row(self, decl: Any, score: float, by_lean: dict[str, tuple[str, dict]]) -> dict[str, Any]:
        return {
            "name": decl.name,
            "kind": decl.kind,
            "signature": decl.signature,
            "doc": decl.doc,
            "library": decl.library,
            "file": decl.file,
            "line": decl.line,
            "score": round(float(score), 4),
            "blueprint": self._blueprint_payload(by_lean.get(decl.name)),
        }

    def search(
        self,
        query: str,
        *,
        mode: str = "text",
        limit: int = 20,
        libraries: list[str] | None = None,
        kinds: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search the workspace's Lean declarations and blueprint statements.

        ``mode``: ``text`` (informal BM25 over Lean *and* blueprint LaTeX), ``name``
        (Loogle-style name match), or ``type`` (signature pattern). ``libraries``
        restricts to one or more projects/libraries; ``kinds`` to declaration kinds
        (theorem/def/…). Each result pairs the Lean declaration with its blueprint
        node (LaTeX) when one links to it; informal search also surfaces blueprint
        nodes not formalised yet, so unproved theorems are findable.
        """
        from archon_horizon.search.index import _BM25, _tokenize

        query = (query or "").strip()
        if not query:
            return {"query": query, "mode": mode, "results": [], "count": 0}
        index = self._get_search_index()
        lib_set = {lib for lib in (libraries or []) if lib} or None
        kind_set = {k for k in (kinds or []) if k} or None
        nodes, by_lean = self._blueprint_nodes(None)

        def _passes(row: dict[str, Any]) -> bool:
            if lib_set is not None and row.get("library") not in lib_set:
                return False
            if kind_set is not None and row.get("kind") not in kind_set:
                return False
            return True

        # Filtering happens after scoring, so over-fetch when a facet is active.
        pool = limit * 8 if (lib_set or kind_set) else limit

        if mode == "name":
            hits = index.search_name(query, limit=pool)
            results = [r for h in hits if _passes(r := self._lean_row(h.declaration, h.score, by_lean))]
        elif mode == "type":
            hits = index.search_type(query, limit=pool)
            results = [r for h in hits if _passes(r := self._lean_row(h.declaration, h.score, by_lean))]
        else:  # informal text: blend Lean declarations and blueprint LaTeX
            query_terms = set(_tokenize(query))

            def _coverage(text: str) -> int:
                tokens = set(_tokenize(text))
                return sum(1 for term in query_terms if term in tokens)

            rows: dict[tuple, dict[str, Any]] = {}
            # Pull a wide candidate pool so coverage re-ranking can promote a
            # declaration that matches ALL the query words above one that just
            # repeats a single common word.
            for hit in index.search_text(query, limit=max(limit * 6, 200)):
                row = self._lean_row(hit.declaration, hit.score, by_lean)
                text = hit.declaration.as_document()
                bp = row.get("blueprint")
                if bp:
                    text = f"{text} {bp.get('title') or ''} {bp.get('statement') or ''}"
                row["coverage"] = _coverage(text)
                rows[("lean", hit.declaration.name)] = row
            if nodes:
                docs = [f"{n.get('title') or ''} {n.get('statement') or ''}" for _, n in nodes]
                for i, score in _BM25(docs).search(query).items():
                    proj, node = nodes[i]
                    lean = node.get("lean")
                    lean_key = ("lean", lean)
                    cov = _coverage(docs[i])
                    if lean and lean_key in rows:
                        rows[lean_key]["score"] = round(max(rows[lean_key]["score"], float(score)), 4)
                        rows[lean_key]["coverage"] = max(rows[lean_key]["coverage"], cov)
                        continue
                    rows[("bp", proj, node.get("id"))] = {
                        "name": lean,
                        "kind": node.get("kind", "blueprint"),
                        "signature": "",
                        "doc": "",
                        "library": proj,
                        "file": "",
                        "line": 0,
                        "score": round(float(score), 4),
                        "coverage": cov,
                        "blueprint": self._blueprint_payload((proj, node)),
                    }
            # Rank: most query words matched first, then blueprint-linked results
            # (the workspace's own theorems with LaTeX), then the BM25 score.
            results = sorted(
                (r for r in rows.values() if _passes(r)),
                key=lambda r: (r.get("coverage", 0), 1 if r.get("blueprint") else 0, r["score"]),
                reverse=True,
            )
        results = results[:limit]
        return {
            "query": query,
            "mode": mode,
            "indexed": len(index.declarations),
            "results": results,
            "count": len(results),
        }

    # ── endpoint registry (shared by live server + static export) ────

    def endpoints(self) -> list[str]:
        """Every GET path the dashboard reads. The live server and the static
        exporter both go through this, so they can never drift."""
        eps = ["/api/state", "/api/transcripts", "/api/git/log", "/api/git/diff", "/api/projects"]
        eps += [f"/api/transcript?ref={t['ref']}" for t in self.transcripts()]
        eps += [f"/api/report?ref={t['ref']}" for t in self.transcripts()]
        for name in self._discover_projects():
            eps.append(f"/api/blueprint/chapters?project={name}")
            eps.append(f"/api/source?project={name}")
            try:
                for f in list_lean_files(self._project_path(name)):
                    eps.append(f"/api/source/file?project={name}&path={f['path']}")
            except KeyError:
                pass
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
        if parsed.path == "/api/report":
            return self.report((query.get("ref") or [""])[0])
        if parsed.path == "/api/search":
            limit_raw = (query.get("limit") or ["20"])[0]
            try:
                limit = max(1, min(100, int(limit_raw)))
            except ValueError:
                limit = 20

            def _csv(key: str) -> list[str]:
                raw = (query.get(key) or [""])[0]
                return [v for v in raw.split(",") if v]

            return self.search(
                (query.get("q") or [""])[0],
                mode=(query.get("mode") or ["text"])[0],
                limit=limit,
                libraries=_csv("libs") or ([(query.get("project") or [""])[0]] if query.get("project") else None),
                kinds=_csv("kinds") or None,
            )
        if parsed.path == "/api/git/log":
            proj = (query.get("project") or [""])[0]
            if proj:
                return get_git_log(self._project_path(proj))
            return {"commits": []}
        if parsed.path == "/api/git/diff":
            proj = (query.get("project") or [""])[0]
            commit = (query.get("commit") or [""])[0]
            if proj and commit:
                return {"diff": get_git_diff(self._project_path(proj), commit)}
            return {"diff": ""}
        if parsed.path == "/api/projects":
            return self.projects_summary()
        if parsed.path == "/api/blueprint/chapters":
            proj = (query.get("project") or [""])[0]
            if proj:
                return project_chapters(self.workspace, proj)
            return project_chapters(self.workspace, next(iter(self._discover_projects()), ""))
        if parsed.path == "/api/source":
            proj = (query.get("project") or [""])[0]
            if proj:
                return {"files": list_lean_files(self._project_path(proj))}
            return {"files": []}
        if parsed.path == "/api/source/file":
            proj = (query.get("project") or [""])[0]
            rel = (query.get("path") or [""])[0]
            if proj and rel:
                return read_lean_file(self._project_path(proj), rel)
            return {"path": rel, "size": 0, "content": ""}
        raise KeyError(path)

    # ── write ───────────────────────────────────────────────────────

    def _inbox_provider(self, provider: str | None, item_id: str | None = None) -> InboxProvider:
        if provider is None and item_id:
            # GitHub mirror ids are ``issue-<n>`` / ``pr-<n>`` (see GithubInboxProvider);
            # local ids are ``I-<n>``. Anything else falls back to local.
            provider = "github" if item_id.startswith(("issue-", "pr-")) else "local"
        if provider in (None, "local"):
            return self.local
        if provider == "github" and self.github:
            return self.github
        if provider == "github":
            raise ValueError("GitHub inbox is not enabled for this workspace")
        raise ValueError(f"unknown inbox provider {provider!r}")

    @staticmethod
    def _record_history(store: Any, item_id: str, actor: str, field: str, *,
                        before: str = "", after: str = "", note: str = "") -> None:
        """Append one transition to an item's history log (best-effort)."""
        try:
            store.append_history(item_id, {
                "at": datetime.now(timezone.utc).isoformat(),
                "actor": (actor or "").strip() or "system",
                "field": field,
                "from": before,
                "to": after,
                "note": note,
            })
        except NotImplementedError:
            pass

    @staticmethod
    def _labels_for_gate(gate: str) -> tuple[str, ...]:
        if gate == "accept":
            return (AGENT_READY,)
        if gate == "pending":
            return (NOT_READY,)
        if gate == "reject":
            return (REJECTED,)
        if gate == "clear":
            return ()
        raise ValueError(f"unknown inbox label gate {gate!r}")

    def edit_inbox(self, action: str, **kw: Any) -> dict[str, Any]:
        if action == "add":
            title = str(kw.get("title") or "").strip()
            comment = str(kw.get("comment") or "").strip()
            author = str(kw.get("author") or "").strip()
            if not title:
                raise ValueError("local inbox title is required")
            if not comment:
                raise ValueError("local inbox first comment is required")
            if not author:
                raise ValueError("local inbox author is required")
            labels = (NOT_READY,) if kw.get("pending") else (AGENT_READY,)
            item = self.local.create_item(
                InboxDraft(
                    kind=InboxKind(kw.get("kind", "hint")),
                    body=f"{title}\n\n{comment}",
                    labels=labels,
                    metadata={"author": author},
                )
            )
            return {"created": item.id}
        if action == "sync":
            provider = self._inbox_provider(kw.get("provider"))
            if "sync" not in provider.capabilities:
                raise ValueError(f"{provider.name} inbox does not support sync")
            result = provider.sync()
            if result.errors:
                return {
                    "ok": False,
                    "provider": result.provider,
                    "imported": result.imported,
                    "updated": result.updated,
                    "skipped": result.skipped,
                    "errors": list(result.errors),
                }
            return {
                "ok": True,
                "provider": result.provider,
                "imported": result.imported,
                "updated": result.updated,
                "skipped": result.skipped,
            }
        item_id = kw["id"]
        provider = self._inbox_provider(kw.get("provider"), item_id)
        actor = str(kw.get("author") or "").strip() or "human"
        if action == "label":
            provider.update_labels(item_id, list(kw["labels"]), actor)
        elif action == "gate":
            provider.update_labels(item_id, list(self._labels_for_gate(str(kw["gate"]))), actor)
        elif action == "comment":
            provider.add_comment(item_id, str(kw["body"]), str(kw.get("author") or "").strip() or None)
        elif action == "body":
            provider.update_body(item_id, str(kw["body"]).strip(), actor)
        elif action == "comment_edit":
            author = str(kw.get("author") or "").strip() if "author" in kw else None
            provider.update_comment(item_id, int(kw["index"]), str(kw["body"]).strip(), author)
        elif action == "kind":
            provider.update_kind(item_id, str(kw["kind"]), actor)
        elif action == "complete":
            provider.update_status(item_id, InboxStatus.CLOSED, actor)
        elif action == "reopen":
            provider.update_status(item_id, InboxStatus.OPEN, actor)
        elif action == "reject":
            provider.update_labels(item_id, [REJECTED], actor)
        elif action == "archive":
            provider.update_status(item_id, InboxStatus.CLOSED, actor)
        elif action == "delete":
            provider.delete_item(item_id)
        else:
            raise ValueError(f"unknown inbox action {action!r}")
        sync_result = None
        if provider is self.github and "sync" in provider.capabilities:
            sync_result = provider.sync()
        return {
            "ok": not (sync_result and sync_result.errors),
            "id": item_id,
            "provider": provider.name,
            "errors": list(sync_result.errors) if sync_result else [],
        }

    def edit_roadmap(self, action: str, **kw: Any) -> dict[str, Any]:
        roadmap = self.stores.roadmap.load()
        items = list(roadmap.items)
        if action == "add":
            item_id = kw.get("id") or f"R-{len(items)+1:04d}"
            author = str(kw.get("author") or "").strip()
            if not author:
                raise ValueError("roadmap item author is required")
            projects = tuple(kw.get("projects", []))
            new_item = RoadmapItem(
                id=item_id,
                title=str(kw.get("title", "")).strip(),
                projects=projects,
                summary=str(kw.get("summary", "")).strip(),
                status=RoadmapStatus(kw.get("status", "active")),
                kind=RoadmapKind(kw.get("kind", "proof")),
                depends_on=tuple(kw.get("depends_on", [])),
                inbox_refs=tuple(kw.get("inbox_refs", [])),
                task_refs=tuple(kw.get("task_refs", [])),
                priority=kw.get("priority", "normal"),
                scope=ItemScope(projects=projects),
                metadata={
                    "author": author,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            )
            items.append(new_item)
            self.stores.roadmap.save(Roadmap(version=roadmap.version, items=tuple(items)))
            self._record_history(self.stores.roadmap, item_id, author, "created",
                                 after=new_item.status.value, note="opened")
            return {"ok": True, "id": item_id}

        item_id = kw["id"]
        idx = next((i for i, item in enumerate(items) if item.id == item_id), None)
        if idx is None:
            raise ValueError(f"roadmap item {item_id} not found")

        if action == "comment":
            body = str(kw.get("body") or "").strip()
            author = str(kw.get("author") or "").strip()
            if not body:
                raise ValueError("comment body is required")
            if not author:
                raise ValueError("comment author is required")
            self.stores.roadmap.add_comment(item_id, body, author)
            return {"ok": True, "id": item_id}

        if action == "delete":
            items.pop(idx)
            self._record_history(self.stores.roadmap, item_id,
                                 str(kw.get("author") or "").strip() or "human", "deleted")
        else:
            old = items[idx]
            actor = str(kw.get("author") or old.metadata.get("author") or "human").strip()
            if action == "status":
                self._record_history(self.stores.roadmap, item_id, actor, "status",
                                     before=str(old.status.value), after=str(kw["status"]))
            elif action == "edit":
                self._record_history(self.stores.roadmap, item_id, actor, "edited",
                                     note="fields updated")
            if action == "edit":
                items[idx] = RoadmapItem(
                    id=old.id,
                    title=str(kw.get("title", old.title)).strip(),
                    projects=tuple(kw.get("projects", old.projects)),
                    summary=str(kw.get("summary", old.summary)).strip(),
                    status=RoadmapStatus(kw.get("status", old.status)),
                    kind=RoadmapKind(kw.get("kind", old.kind)),
                    depends_on=tuple(kw.get("depends_on", old.depends_on)),
                    inbox_refs=tuple(kw.get("inbox_refs", old.inbox_refs)),
                    task_refs=tuple(kw.get("task_refs", old.task_refs)),
                    priority=kw.get("priority", old.priority),
                    scope=ItemScope(projects=tuple(kw.get("projects", old.projects))),
                    metadata={
                        **old.metadata, 
                        "author": kw.get("author", old.metadata.get("author", "unknown")),
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }
                )
            elif action == "status":
                items[idx] = RoadmapItem(
                    id=old.id, title=old.title, projects=old.projects, summary=old.summary,
                    status=RoadmapStatus(kw["status"]), kind=old.kind, depends_on=old.depends_on,
                    inbox_refs=old.inbox_refs, task_refs=old.task_refs, priority=old.priority, 
                    scope=old.scope,
                    metadata={
                        **old.metadata,
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }
                )
            else:
                raise ValueError(f"unknown roadmap action {action!r}")
        
        self.stores.roadmap.save(Roadmap(version=roadmap.version, items=tuple(items)))
        return {"ok": True, "id": item_id}

    def edit_task(self, action: str, **kw: Any) -> dict[str, Any]:
        tasks = {task.id: task for task in self.stores.tasks.list()}
        if action == "add":
            task_id = self._slug(str(kw.get("id") or kw.get("name") or kw.get("title") or "task"))
            if task_id in tasks:
                raise ValueError(f"task {task_id!r} already exists")
            projects = tuple(kw.get("projects", []))
            if not projects:
                raise ValueError("task requires at least one project")
            if not str(kw.get("author") or "").strip():
                raise ValueError("task author is required")
            task = self._task_from_payload(task_id, projects[0], kw)
            self.stores.tasks.put(task)
            self._record_history(self.stores.tasks, task_id, task.metadata.get("author", ""),
                                 "created", after=task.status.value, note="opened")
            return {"ok": True, "id": task_id}

        task_id = str(kw["id"])
        old = tasks.get(task_id)
        if old is None:
            raise ValueError(f"task {task_id} not found")
        actor = str(kw.get("author") or old.metadata.get("author") or "human").strip()
        if action == "comment":
            body = str(kw.get("body") or "").strip()
            author = str(kw.get("author") or "").strip()
            if not body:
                raise ValueError("comment body is required")
            if not author:
                raise ValueError("comment author is required")
            self.stores.tasks.add_comment(task_id, body, author)
            return {"ok": True, "id": task_id}
        if action == "delete":
            self.stores.tasks.delete(task_id)
            return {"ok": True, "id": task_id}
        if action == "status":
            self.stores.tasks.put(dataclasses.replace(old, status=TaskStatus(kw["status"]), updated_at=datetime.now(timezone.utc)))
            self._record_history(self.stores.tasks, task_id, actor, "status",
                                 before=old.status.value, after=str(kw["status"]))
            return {"ok": True, "id": task_id}
        if action != "edit":
            raise ValueError(f"unknown task action {action!r}")

        projects = tuple(kw.get("projects", old.projects or ((old.project,) if old.project else ())))
        project = projects[0] if projects else old.project
        self.stores.tasks.put(self._task_from_payload(task_id, project, kw, old=old))
        self._record_history(self.stores.tasks, task_id, actor, "edited", note="fields updated")
        return {"ok": True, "id": task_id}

    def _task_from_payload(
        self, task_id: str, project: str, kw: dict[str, Any], old: HorizonTask | None = None
    ) -> HorizonTask:
        projects = tuple(kw.get("projects", old.projects if old else (project,)))
        title = str(kw.get("title", old.title if old else task_id)).strip()
        explanation = str(kw.get("explanation", old.explanation if old else "")).strip()
        files = tuple(kw.get("files", old.write_set.files if old else ()))
        write_projects = tuple(kw.get("write_projects", old.write_set.projects if old else projects))
        declarations = tuple(kw.get("declarations", old.write_set.declarations if old else ()))
        blueprint_nodes = tuple(kw.get("blueprint_nodes", old.write_set.blueprint_nodes if old else ()))
        now = datetime.now(timezone.utc)
        author = str(kw.get("author", (old.metadata.get("author") if old else "") or "")).strip()
        metadata = {**(old.metadata if old else {}), "updated_at": now.isoformat()}
        if author:
            metadata["author"] = author
        if not old:
            metadata.setdefault("created_at", now.isoformat())
        return HorizonTask(
            id=task_id,
            project=project,
            objective=explanation or title,
            title=title,
            explanation=explanation,
            projects=projects,
            priority=str(kw.get("priority", old.priority if old else "normal")),
            status=TaskStatus(kw.get("status", old.status if old else TaskStatus.QUEUED)),
            write_set=WriteSet(
                files=files,
                projects=write_projects,
                declarations=declarations,
                blueprint_nodes=blueprint_nodes,
                workspace=bool(kw.get("workspace", old.write_set.workspace if old else False)),
            ),
            scope=ItemScope(
                projects=write_projects,
                files=files,
                declarations=declarations,
                blueprint_nodes=blueprint_nodes,
            ),
            roadmap_refs=tuple(kw.get("roadmap_refs", old.roadmap_refs if old else ())),
            inbox_refs=tuple(kw.get("inbox_refs", old.inbox_refs if old else ())),
            artifact_refs=old.artifact_refs if old else (),
            created_at=old.created_at if old else now,
            updated_at=now,
            metadata=metadata,
        )

    @staticmethod
    def _slug(value: str) -> str:
        slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
        while "--" in slug:
            slug = slug.replace("--", "-")
        return slug or "task"
