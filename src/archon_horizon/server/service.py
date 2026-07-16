"""Live workspace service — the data layer behind the server.

Pure-ish application logic with no socket/HTTP concerns, so it is testable
directly. Unlike the static dashboard, this exposes inbox editing: the
roadmap permits the *live local* dashboard to edit the local inbox (the
static page never persists edits).
"""

from __future__ import annotations

import dataclasses
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from datetime import datetime, timezone

from archon_horizon.log import log

from archon_horizon.blueprint.chapters import project_chapters
from archon_horizon.blueprint.checks import is_countable
from archon_horizon.blueprint.workspace import published_dag, published_dags, workspace_dags, workspace_dags_rich
from archon_horizon.config.loader import build_stores, build_workspace, load_config
from archon_horizon.core.inbox import InboxDraft, InboxKind, InboxStatus
from archon_horizon.core.labels import AGENT_READY, NOT_READY, REJECTED
from archon_horizon.core.roadmap import Roadmap, RoadmapItem, RoadmapKind, RoadmapStatus, apply_hierarchy
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.status_sync import roadmap_status_for_task_status
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.inboxes.filesystem import FilesystemInboxProvider
from archon_horizon.inboxes.github import GithubInboxProvider
from archon_horizon.runlog import RunLog, SessionLog
from archon_horizon.store import serde
from archon_horizon.transcript.parsers import observed_effort, observed_model
from archon_horizon.transcript.sink import read_transcript
from archon_horizon.transcript.subagents import materialize_subagent_sessions
from archon_horizon.server.git_api import get_git_log, get_git_diff
from archon_horizon.server.source_api import file_stats, list_lean_files, read_lean_file
from archon_horizon.vcs.git import WorkspaceGit, git_available


# Per-node blueprint fields that carry full text (a declaration's LaTeX statement,
# its proof source, and its whole Lean source). They dominate the DAG payload —
# often >80% of it — but are only needed on the Blueprint/DAG detail views, which
# fetch the full DAG on demand (/api/blueprint/dag). Stripping them from the 5s
# state poll keeps that payload small while leaving the light fields the Lean page
# and the graph itself need (ids, names, files, status, metrics, edges).
_HEAVY_NODE_FIELDS = ("statement", "proof_tex", "lean_source")


def _light_dags(dags: dict[str, Any]) -> dict[str, Any]:
    """A copy of the published DAGs with the heavy per-node text fields dropped."""
    out: dict[str, Any] = {}
    for name, dag in dags.items():
        if not isinstance(dag, dict):
            out[name] = dag
            continue
        light = dict(dag)
        nodes = dag.get("nodes")
        if isinstance(nodes, list):
            light["nodes"] = [
                {k: v for k, v in node.items() if k not in _HEAVY_NODE_FIELDS}
                if isinstance(node, dict) else node
                for node in nodes
            ]
        out[name] = light
    return out


def _flatten_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for session in sessions:
        out.append(session)
        out.extend(_flatten_sessions(session.get("children", [])))
    return out


def _agentic_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Top-level sessions that should determine the run's current status."""
    out: list[dict[str, Any]] = []
    for session in sessions:
        role = str(session.get("meta", {}).get("role") or "").strip().lower()
        name = str(session.get("session") or "")
        if role == "system" or name.endswith("-system"):
            continue
        out.append(session)
    return out


# A "running" session is only genuinely active if its process is alive or it
# emitted activity recently. This window covers concurrent runs (which the run
# lock doesn't track) and a quiet-but-alive engine (e.g. a long Lean build);
# past it, a still-"running" session with a dead process reads as interrupted.
_ACTIVE_WINDOW_S = 300.0
_PROJECT_HISTORY_LIMIT = 10
_PROJECT_HISTORY_MAX_LIMIT = 100


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


_SESSION_CACHE_VERSION = 1


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
        # ── poll caches ──────────────────────────────────────────────
        # The dashboard polls /api/state every 5s; on a large workspace a naive
        # poll re-parses hundreds of MB of transcripts. Completed sessions never
        # change, so their derived state is cached by file signature (mtime+size
        # of transcript & meta) — in memory and persisted under
        # ``<state_dir>/cache/`` so a server restart stays fast. The cache dir is
        # excluded from the ledger and from the /api/state change stamp.
        self._session_cache: dict[str, dict[str, Any]] = {}
        self._session_cache_lock = threading.Lock()
        self._session_cache_dirty = False
        self._session_cache_saved_at = 0.0
        self._load_session_cache()
        # events.jsonl is append-only: keep the parsed events plus the byte
        # offset consumed, and parse only the appended tail on each poll.
        self._events_lock = threading.Lock()
        self._events_offset = 0
        self._events_sig: tuple[int, int] | None = None
        self._events: list[Any] = []
        # Published blueprint DAGs change only on an explicit sync/publish;
        # cache the lightened copy by the cache files' signatures.
        self._dags_sig: tuple | None = None
        self._dags_light: dict[str, Any] = {}
        # Per-collection payload caches (tasks / roadmap / inboxes), keyed by a
        # stat-walk stamp of their directory: hundreds of per-item YAML/JSON
        # loads become one walk when nothing changed.
        self._subtree_cache: dict[str, tuple[str, Any]] = {}

    def _session_cache_path(self) -> Path:
        return self.workspace.state_path / "cache" / "session-states.json"

    def _load_session_cache(self) -> None:
        try:
            raw = json.loads(self._session_cache_path().read_text("utf-8"))
        except (OSError, ValueError):
            return
        if raw.get("version") != _SESSION_CACHE_VERSION:
            return
        sessions = raw.get("sessions")
        if isinstance(sessions, dict):
            self._session_cache = sessions

    def _save_session_cache(self, *, min_interval_s: float = 30.0) -> None:
        """Persist the session cache (atomically), rate-limited and only when
        new entries were computed since the last save."""
        with self._session_cache_lock:
            if not self._session_cache_dirty:
                return
            if time.monotonic() - self._session_cache_saved_at < min_interval_s:
                return
            snapshot = dict(self._session_cache)
            self._session_cache_dirty = False
            self._session_cache_saved_at = time.monotonic()
        path = self._session_cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"version": _SESSION_CACHE_VERSION, "sessions": snapshot}),
                "utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass  # the cache is an optimization; never fail a poll over it

    @staticmethod
    def _stat_sig(path: Path) -> list[int]:
        try:
            st = path.stat()
        except OSError:
            return [0, 0]
        return [st.st_mtime_ns, st.st_size]

    def _session_sig(self, session: SessionLog) -> list[int]:
        return self._stat_sig(session.transcript_path) + self._stat_sig(session.path / "meta.json")

    # ── read ────────────────────────────────────────────────────────

    def _events_all(self) -> list[Any]:
        """All events, parsed incrementally: ``events.jsonl`` is append-only, so
        after the first full read each poll parses only the appended tail."""
        path = self.workspace.state_path / "events.jsonl"
        with self._events_lock:
            try:
                st = path.stat()
            except OSError:
                self._events_offset, self._events_sig, self._events = 0, None, []
                return []
            sig = (st.st_mtime_ns, st.st_size)
            if sig == self._events_sig:
                return self._events
            if st.st_size < self._events_offset:
                # Truncated/rewritten (should not happen for an append-only log):
                # fall back to a full re-read.
                self._events_offset, self._events = 0, []
            with path.open("rb") as handle:
                handle.seek(self._events_offset)
                chunk = handle.read()
            # Only consume up to the last newline, so a line mid-append is left
            # for the next poll instead of being parsed as a broken record.
            cut = chunk.rfind(b"\n")
            if cut < 0:
                self._events_sig = sig
                return self._events
            for line in chunk[: cut + 1].decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._events.append(serde.event_from_dict(json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue
            self._events_offset += cut + 1
            self._events_sig = sig
            return self._events

    _STAMP_SKIP_DIRS = frozenset({"vcs", "locks", "cache", "search"})

    def state_stamp(self) -> str:
        """A cheap change stamp over everything ``state()`` reads: config.yaml
        plus the state tree (minus the ledger, caches, and the search index).
        The HTTP layer uses it as the /api/state ETag so an unchanged poll is a
        ~1ms 304 instead of a full state recompute."""
        max_mtime = 0
        count = 0
        total = 0
        for path in (self.root / "config.yaml",):
            try:
                st = path.stat()
                max_mtime = max(max_mtime, st.st_mtime_ns)
                count += 1
                total += st.st_size
            except OSError:
                pass
        state_dir = self.workspace.state_path
        for dirpath, dirnames, filenames in os.walk(state_dir):
            if Path(dirpath) == state_dir:
                dirnames[:] = [d for d in dirnames if d not in self._STAMP_SKIP_DIRS]
            for name in filenames:
                try:
                    st = os.stat(os.path.join(dirpath, name))
                except OSError:
                    continue
                max_mtime = max(max_mtime, st.st_mtime_ns)
                count += 1
                total += st.st_size
        return f"{max_mtime}-{count}-{total}"

    @staticmethod
    def _subtree_stamp(*roots: Path) -> str:
        max_mtime = 0
        count = 0
        total = 0
        for root in roots:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    try:
                        st = os.stat(os.path.join(dirpath, name))
                    except OSError:
                        continue
                    max_mtime = max(max_mtime, st.st_mtime_ns)
                    count += 1
                    total += st.st_size
        return f"{max_mtime}-{count}-{total}"

    def _cached_by_stamp(self, key: str, roots: tuple[Path, ...], compute) -> Any:
        stamp = self._subtree_stamp(*roots)
        cached = self._subtree_cache.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        value = compute()
        self._subtree_cache[key] = (stamp, value)
        return value

    def _light_dags_cached(self) -> dict[str, Any]:
        """The lightened published DAGs, re-read only when a cache file under
        ``<state_dir>/blueprints`` changes (they change on publish/sync only)."""
        blueprint_dir = self.workspace.state_path / "blueprints"
        sig = tuple(
            (p.name, *self._stat_sig(p)) for p in sorted(blueprint_dir.glob("*.json"))
        )
        if sig != self._dags_sig:
            self._dags_light = _light_dags(published_dags(self.workspace))
            self._dags_sig = sig
        return self._dags_light

    def state(self, *, events_tail: int = 50) -> dict[str, Any]:
        events = self._events_all()
        return {
            "workspace": self.workspace.name,
            "workspace_root": self.workspace.root.as_posix(),
            # The workspace's Horizon state/config directory (.archon-horizon),
            # surfaced so the dashboard can show where this workspace's config,
            # runs, and ledger live.
            "config_dir": self.workspace.state_path.as_posix(),
            "roadmap": self._cached_by_stamp(
                "roadmap",
                (self.workspace.state_path / "roadmap",),
                lambda: serde.to_jsonable(self.stores.roadmap.load()),
            ),
            "tasks": self._cached_by_stamp(
                "tasks",
                (self.workspace.state_path / "tasks",),
                lambda: [serde.to_jsonable(t) for t in self.stores.tasks.list()],
            ),
            "runs": self._runs_state(events),
            "local_inbox": self._cached_by_stamp(
                "local_inbox",
                (self.workspace.state_path / "inbox" / "local",),
                lambda: [serde.to_jsonable(i) for i in self.local.list_items()],
            ),
            "github_inbox": self._cached_by_stamp(
                "github_inbox",
                (self.workspace.state_path / "inbox" / "github",),
                lambda: [serde.to_jsonable(i) for i in self.github.list_items()] if self.github else [],
            ),
            "inbox_providers": self._inbox_provider_state(),
            "memory": self.stores.memory.load(),
            "reports": self.stores.reports.list(),
            # Light DAGs only (heavy statement/proof/lean_source stripped): the
            # Blueprint and DAG pages fetch the full per-project DAG on demand via
            # /api/blueprint/dag, so the 5s poll doesn't re-ship several MB of text.
            "blueprints": self._light_dags_cached(),
            "projects": self._discover_projects(),
            "libraries": self._search_libraries(),
            "harnesses": self._harness_state(),
            "events": [serde.to_jsonable(e) for e in events[-events_tail:]],
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

    def _project_rel_path(self, name: str) -> str:
        project_path = self._project_path(name).resolve()
        root = self.workspace.root.resolve()
        if project_path == root:
            return ""
        return project_path.relative_to(root).as_posix()

    def _project_history(self, name: str, *, limit: int = _PROJECT_HISTORY_LIMIT) -> list[dict[str, Any]]:
        if not git_available():
            return []
        git = WorkspaceGit(self.workspace.root)
        if not git.is_repo():
            return []
        try:
            rel = self._project_rel_path(name)
        except (KeyError, ValueError):
            return []
        paths = (rel,) if rel else ()
        commit_limit = max(1, min(_PROJECT_HISTORY_MAX_LIMIT, int(limit)))
        commits = list(reversed(git.log(paths=paths, limit=commit_limit)))
        history: list[dict[str, Any]] = []
        for commit in commits:
            sha = commit.get("sha") or ""
            if not sha:
                continue
            lean_files = [p for p in git.files_at(sha, paths) if p.endswith(".lean")]
            sorries = 0
            loc = 0
            loc_code = 0
            for path in lean_files:
                text = git.file_at(sha, path)
                if text is None:
                    continue
                stats = file_stats(text)
                sorries += stats["sorries"]
                loc += stats["loc"]
                loc_code += stats["loc_code"]
            history.append({
                "sha": sha,
                "short_sha": commit.get("short_sha") or sha[:7],
                "date": commit.get("date") or "",
                "subject": commit.get("subject") or "",
                "lean_files": len(lean_files),
                "loc": loc,
                "loc_code": loc_code,
                "sorries": sorries,
            })
        return history

    def projects_summary(self) -> dict[str, Any]:
        """Per-project metrics for the overview: Lean LOC/sorries + blueprint size."""
        dags = workspace_dags(self.workspace)
        projects: list[dict[str, Any]] = []
        for name in self._discover_projects():
            try:
                project_path = self._project_path(name)
                files = list_lean_files(project_path)
            except KeyError:
                project_path = self.workspace.root / name
                files = []
            dag = dags.get(name) or {}
            # Count only formalisation obligations (theorems/lemmas/defs, …);
            # prose nodes like remarks carry no \lean obligation, so they must
            # not inflate the blueprint "todo"/coverage totals.
            nodes = [n for n in dag.get("nodes", []) if is_countable(n)]
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
                "config_dir": cfg.config_dir,
                "options": dict(cfg.options),
            }
            for name, cfg in self.cfg.harnesses.items()
        }

    def _runs_state(self, events: list[Any] | None = None) -> list[dict[str, Any]]:
        records = {run.id: serde.to_jsonable(run) for run in self.stores.runs.list()}
        run_events: dict[str, list[dict[str, Any]]] = {}
        for event in (events if events is not None else self._events_all()):
            run_id = event.data.get("run_id")
            if isinstance(run_id, str) and run_id:
                run_events.setdefault(run_id, []).append(serde.to_jsonable(event))
        states = [
            self._run_state(
                self.stores.run_logs.get(run_id),
                records.get(run_id, {}),
                run_events.get(run_id, []),
            )
            for run_id in reversed(self.stores.run_logs.ids())
        ]
        self._save_session_cache()
        return states

    def _run_state(
        self,
        run: RunLog,
        record: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        sessions = [self._session_state(run.id, session, "") for session in run.sessions()]
        baseline_session = self._baseline_session_state(run.id, events)
        if baseline_session is not None:
            sessions.insert(0, baseline_session)
        # Tag each top-level session with the ledger commit it produced (from its
        # integration event), so the Logs view can offer a per-session change view.
        integrated = {
            d.get("session"): d
            for event in events
            if event.get("type") == "workspace.session.integrated"
            for d in [event.get("data", {})]
            if d.get("session")
        }
        for session in sessions:
            info = integrated.get(session["session"])
            if info:
                session["workspace_commit"] = info.get("workspace_commit")
                session["change_projects"] = [p for p in (info.get("projects") or []) if p]
        flat = _flatten_sessions(sessions)
        # A terminal `run.stopped` event means the orchestrator is gone, so no
        # session is still live regardless of timing. Absent that (a hard crash),
        # a run with "running" sessions is only truly active when a session
        # emitted activity recently.
        run_ended = any(event.get("type") == "run.stopped" for event in events)
        status_basis = _agentic_sessions(sessions) or flat
        latest_status_session = status_basis[-1] if status_basis else None
        active = (
            latest_status_session is not None
            and latest_status_session["status"] == "running"
            and not run_ended
            and _is_recent(latest_status_session.get("last_at", ""))
        )
        active_session_id = id(latest_status_session) if active else None
        for session in flat:
            if session["status"] == "running" and id(session) != active_session_id:
                session["status"] = "interrupted"
        last_status = latest_status_session["status"] if latest_status_session else "created"
        if last_status == "running" and not active:
            last_status = "interrupted"
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
                last_status if last_status in (
                    "running",
                    "interrupted",
                    "timed_out",
                    "throttled",
                    "failed",
                    "completed",
                )
                else "created"
            ),
            "rounds_completed": len(rounds_done),
            "session_count": len(flat),
            "usage": usage,
            "stop": next((event["data"] for event in reversed(events) if event.get("type") == "run.stopped"), None),
            "sessions": sessions,
        }

    def _session_state(self, run_id: str, session: SessionLog, parent: str) -> dict[str, Any]:
        """One session's derived state, served from the signature-keyed cache.

        A completed session's transcript and meta never change, so after the
        first computation each poll costs two ``stat`` calls per session. The
        cached node excludes ``children`` (rebuilt from disk every poll so a
        live session's subagents appear as they materialize) and is returned as
        a shallow copy because ``_run_state`` annotates/flips top-level keys.
        """
        key = str(session.path)
        sig = self._session_sig(session)
        with self._session_cache_lock:
            cached = self._session_cache.get(key)
        if cached is not None and cached.get("sig") == sig:
            node = dict(cached["node"])
        else:
            node = self._compute_session_state(session)
            # Re-sign AFTER computing: a live materialization may have grown the
            # session dir, and computing from a transcript mid-append must not be
            # remembered under the newer signature.
            sig_after = self._session_sig(session)
            if sig_after == sig:
                with self._session_cache_lock:
                    self._session_cache[key] = {"sig": sig, "node": dict(node)}
                    self._session_cache_dirty = True
        node["run"] = run_id
        node["parent"] = parent
        node["children"] = [
            self._session_state(run_id, child, session.name) for child in session.subsessions()
        ]
        return node

    def _compute_session_state(self, session: SessionLog) -> dict[str, Any]:
        # Fold any inline subagent events into on-disk child sessions. The write
        # path (harness end) normally does this; running it here covers live
        # sessions mid-stream and older/crashed sessions that were never
        # materialized. It happens only on a cache miss — once per session per
        # server lifetime — never on every poll.
        try:
            materialize_subagent_sessions(session.path)
        except Exception:
            pass
        events = read_transcript(session.transcript_path) if session.transcript_path.exists() else []
        end = next((event for event in reversed(events) if event.kind == "session_end"), None)
        usage = _sum_usage(event.usage for event in events if event.usage is not None)
        meta = session.read_meta()

        def _fail_status(reason: Any, timed_out: Any) -> str:
            # A timeout or an exhausted-retry transient error is NOT a crash: give
            # it its own status so the UI can show a "timed out"/"throttled" glyph
            # rather than the same ✕ as a genuine failure.
            if timed_out:
                return "timed_out"
            if reason in ("overloaded", "server_error", "rate_limit"):
                return "throttled"
            return "failed"

        status = "created"
        if end is not None:
            status = ("completed" if end.data.get("ok", True)
                      else _fail_status(end.data.get("failure_reason"),
                                        meta.get("timed_out") or end.data.get("timed_out")))
        elif "ok" in meta:
            status = "completed" if meta.get("ok") else _fail_status(meta.get("failure_reason"), meta.get("timed_out"))
        elif isinstance(meta.get("data"), dict) and "ok" in meta["data"]:
            data = meta["data"]
            status = "completed" if data.get("ok") else _fail_status(data.get("failure_reason"), data.get("timed_out"))
        elif any(event.kind == "error" for event in events):
            status = "failed"
        elif meta.get("status") == "running":
            status = "running"
        elif events:
            # Has streamed events but no terminal marker: still live (this covers a
            # session mid-retry-backoff, whose only recent event is a NOTICE).
            status = "running"
        elif meta:
            status = "completed"
        ref = session.transcript_path.relative_to(self.root).as_posix() if session.transcript_path.exists() else ""
        return {
            "session": session.name,
            "ref": ref,
            "meta": meta,
            "status": status,
            "model": observed_model(events) or meta.get("model"),
            # Prefer the effort read back from the engine's own stream (codex stamps
            # it on session-meta from turn_context) over the configured tier, so the
            # UI confirms what the session actually ran with — matching model above.
            "effort": observed_effort(events) or meta.get("effort"),
            "started_at": events[0].at.isoformat() if events else "",
            "ended_at": end.at.isoformat() if end else "",
            "last_at": events[-1].at.isoformat() if events else "",
            "usage": usage,
        }

    @staticmethod
    def _baseline_session_state(run_id: str, events: list[dict[str, Any]]) -> dict[str, Any] | None:
        baseline = next(
            (event for event in events if event.get("type") == "workspace.run_baseline"),
            None,
        )
        if baseline is None:
            return None
        data = baseline.get("data", {})
        created_at = str(baseline.get("created_at") or "")
        sha = data.get("sha")
        return {
            "run": run_id,
            "session": "0000-system-baseline",
            "parent": "",
            "ref": "",
            "meta": {
                "role": "system",
                "name": "run baseline",
                "workspace_sha": sha,
                "projects": data.get("projects") or [],
            },
            "status": "completed" if sha else "failed",
            "model": None,
            "effort": None,
            "started_at": created_at,
            "ended_at": created_at,
            "last_at": created_at,
            "usage": _sum_usage(()),
            "children": [],
            "workspace_commit": sha,
            "change_projects": [p for p in (data.get("projects") or []) if p],
            "synthetic": True,
        }

    def _session_integrations(self, run_id: str) -> dict[str, dict[str, Any]]:
        """Map each session name in ``run_id`` to its integration event data
        (the ledger commit sha + the projects it was scoped to)."""
        integrated: dict[str, dict[str, Any]] = {}
        for raw in self.stores.events.read_all():
            event = serde.to_jsonable(raw)
            data = event.get("data", {})
            if event.get("type") == "workspace.session.integrated" and data.get("run_id") == run_id:
                name = data.get("session")
                if name:
                    integrated[name] = data
        return integrated

    def _run_baseline_commit(self, run_id: str) -> str | None:
        for raw in reversed(self.stores.events.read_all()):
            event = serde.to_jsonable(raw)
            data = event.get("data", {})
            if event.get("type") == "workspace.run_baseline" and data.get("run_id") == run_id:
                sha = data.get("sha")
                return str(sha) if sha else None
        return None

    def _last_run_commit(self, run_id: str) -> str | None:
        last_commit: str | None = None
        integrated = self._session_integrations(run_id)
        try:
            run = self.stores.run_logs.get(run_id)
            for session in run.sessions():
                info = integrated.get(session.name)
                if info and info.get("workspace_commit"):
                    last_commit = info["workspace_commit"]
        except Exception:
            pass
        return last_commit or self._run_baseline_commit(run_id)

    def _session_live_context(self, run_id: str, session_name: str | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            run = self.stores.run_logs.get(run_id)
        except Exception:
            return (), ()
        selected: SessionLog | None = None
        if session_name:
            selected = next((s for s in run.sessions() if s.name == session_name), None)
        if selected is None:
            selected = next((s for s in reversed(run.sessions()) if self._session_state(run_id, s, "")["status"] == "running"), None)
        meta = selected.read_meta() if selected is not None else {}
        projects = [p for p in (meta.get("projects") or []) if p]
        project_paths = self._project_paths(projects) if projects else tuple(
            self.workspace.project_path(n).relative_to(self.root).as_posix()
            for n in self.workspace.projects
        )
        dirty_at_start = tuple(str(p) for p in (meta.get("dirty_at_start") or []) if p)
        return project_paths, dirty_at_start

    def _project_paths(self, names: list[str]) -> tuple[str, ...]:
        paths: list[str] = []
        for name in names:
            if name in self.workspace.projects:
                paths.append(self.workspace.project_path(name).relative_to(self.root).as_posix())
        return tuple(paths)

    def run_changes(self, run_id: str) -> dict[str, Any]:
        """Deterministic per-session change view for one run: what each session's
        ledger commit changed (per-file LOC/sorry before→after), plus cumulative
        run and per-file trends. Computed from git + the sorry/LOC scanner — no
        AI. Precomputed by the static exporter, so the static dashboard shows the
        same boxes/graphs. Per-file diffs are fetched lazily (see the
        ``/api/session/file-diff`` endpoint)."""
        from archon_horizon.server.changes_api import aggregate_session_summary, session_change_summary
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        try:
            run = self.stores.run_logs.get(run_id)
        except Exception:
            return {"run": run_id, "sessions": [], "trend": [], "file_trends": {}}

        integrated = self._session_integrations(run_id)
        wsgit = WorkspaceGit(self.root) if git_available() else None

        sessions_out: list[dict[str, Any]] = []
        trend: list[dict[str, Any]] = []
        # Per-file sorry trajectory across the sessions where the file changed.
        file_trends: dict[str, list[dict[str, Any]]] = {}
        cum_sorry = 0
        # Diff each session against its commit's own git PARENT — the state of the
        # ledger immediately before this commit — which is exactly what git
        # records the commit as changing. The workspace ledger is ONE shared
        # branch that every run commits onto, so "the previous session of the same
        # run" is meaningless: dozens of other runs' commits (and dashboard
        # publishes) interleave on that branch, and diffing across them would
        # report hundreds of unrelated files. The parent is always the right,
        # deterministic base (base=None → session_change_summary uses commit^).
        for session in run.sessions():
            info = integrated.get(session.name) or {}
            projects = [p for p in (info.get("projects") or []) if p]
            paths = self._project_paths(projects) or tuple()
            # Agent sessions are attributed only from agent-authored semantic
            # commits. The orchestrator's integration sweep is deterministic
            # ledger bookkeeping: it can capture unrelated project files that
            # changed in the shared worktree, so it must not populate the agent
            # file table. System sessions still report their deterministic commits.
            commit_rows = wsgit.session_commits_detailed(run_id, session.name) if wsgit is not None else []
            role = str(info.get("role") or session.read_meta().get("role") or "").lower()
            agent_role = role in ("ground", "horizon")
            agent_commits = [
                (r["sha"], r["subject"]) for r in commit_rows
                if r.get("kind") == "agent" and str(r.get("role") or role).lower() in ("ground", "horizon", "")
            ]
            deterministic_rows = [r for r in commit_rows if r.get("kind") != "agent"]
            if agent_role:
                if agent_commits:
                    summary = aggregate_session_summary(self.root, paths, agent_commits)
                    summary["change_source"] = "agent-commits"
                elif info:
                    summary = session_change_summary(self.root, None, paths, base=None, fallback_files=())
                    summary["change_source"] = "no-agent-commits"
                else:
                    continue
            else:
                deterministic_commits = [(r["sha"], r["subject"]) for r in deterministic_rows or commit_rows]
                if deterministic_commits:
                    summary = aggregate_session_summary(self.root, paths, deterministic_commits)
                    summary["change_source"] = "deterministic-commits"
                elif info.get("workspace_commit"):
                    summary = session_change_summary(
                        self.root, info.get("workspace_commit"), paths,
                        base=None, fallback_files=tuple(info.get("files") or ()),
                    )
                    summary["change_source"] = "integration-fallback"
                elif info:
                    summary = session_change_summary(
                        self.root, None, paths, base=None, fallback_files=tuple(info.get("files") or ()),
                    )
                    summary["change_source"] = "integration-fallback"
                else:
                    continue
            summary["session"] = session.name
            summary["role"] = role or info.get("role")
            summary["projects"] = projects
            summary["system_commits"] = [
                {"sha": r["sha"], "subject": r["subject"], "kind": r.get("kind", "")}
                for r in deterministic_rows
            ]
            # Advisory task write-set, kept for API compatibility/context only.
            # The file table itself comes from provenance-tagged commits, not this
            # expected-file list.
            summary["scope_files"] = self._session_scope_files(info.get("task_id"))
            sessions_out.append(summary)
            cum_sorry += summary.get("sorry_delta", 0)
            trend.append({
                "session": session.name,
                "role": info.get("role"),
                "sorry_delta": summary.get("sorry_delta", 0),
                "cumulative_sorry_delta": cum_sorry,
                "loc_code_delta": summary.get("lean", {}).get("loc_code_delta", 0),
            })
            for f in summary.get("files", []):
                if f.get("category") in ("lean", "blueprint") and "sorry_after" in f:
                    file_trends.setdefault(f["path"], []).append({
                        "session": session.name,
                        "sorry_after": f.get("sorry_after", 0),
                        "loc_code_after": f.get("loc_code_after", 0),
                    })
        return {"run": run_id, "sessions": sessions_out, "trend": trend, "file_trends": file_trends}

    def _session_scope_files(self, task_id: Any) -> list[str]:
        """The write-set files a task declared, or [] (unknown / project-scoped)."""
        if not task_id:
            return []
        try:
            task = self.stores.tasks.get(str(task_id))
        except Exception:
            return []
        files = getattr(getattr(task, "write_set", None), "files", ()) or ()
        return [str(f) for f in files]

    def working_changes(self, run_id: str, session: str | None = None) -> dict[str, Any]:
        """Live change view for a still-running session: the current working tree
        (uncommitted) vs the run's last committed session. Live-only (no working
        tree exists in a static export).

        Scoped to the files this run's own agent commits have touched, so a
        *parallel* run writing sibling files in the same project (the shared
        worktree) does not leak into this run's diff. Until this run has any agent
        commit yet (its first session, nothing committed) we fall back to the
        session's project scope so the very first uncommitted work is still shown.
        """
        from archon_horizon.server.changes_api import session_change_summary
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        last_commit = self._last_run_commit(run_id)
        project_paths, dirty_at_start = self._session_live_context(run_id, session)
        run_files: set[str] = set()
        if git_available():
            run_files = WorkspaceGit(self.root).run_agent_changed_files(run_id)
        # Keep only files under the session's project dirs — the agent commit also
        # carries shared-state bookkeeping (.archon-horizon/…) that the old
        # project-scoped live view never showed; drop it here too.
        if run_files and project_paths:
            run_files = {
                f for f in run_files
                if any(f == p or f.startswith(p.rstrip("/") + "/") for p in project_paths)
            }
        scope_paths = tuple(sorted(run_files)) if run_files else project_paths
        summary = session_change_summary(
            self.root,
            None,
            scope_paths,
            base=last_commit,
            worktree=True,
            exclude_paths=dirty_at_start,
        )
        summary["run"] = run_id
        summary["session"] = session or ""
        summary["scope_source"] = "run-agent-files" if run_files else "project"
        return summary

    def session_commits_view(self, run_id: str, session: str) -> dict[str, Any]:
        """Per-COMMIT change view for one session: each commit the session made
        (message + per-file table vs that commit's own git parent), so the
        dashboard can show progress at *commit* granularity — the commit message
        and diff ARE the progress record. Complements ``run_changes`` (which sums a
        session's commits into one table). Per-commit file diffs are fetched lazily
        via ``/api/session/file-diff?...&sha=<sha>``."""
        from archon_horizon.server.changes_api import session_change_summary
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        wsgit = WorkspaceGit(self.root) if git_available() else None
        if wsgit is None:
            return {"run": run_id, "session": session, "commits": []}
        info = self._session_integrations(run_id).get(session) or {}
        projects = [p for p in (info.get("projects") or []) if p]
        paths = self._project_paths(projects) or tuple()
        commits_out: list[dict[str, Any]] = []
        for r in wsgit.session_commits_detailed(run_id, session):
            sha = r["sha"]
            summary = session_change_summary(self.root, sha, paths, base=None)
            commits_out.append({
                "sha": sha,
                "short_sha": sha[:10],
                "subject": r.get("subject", ""),
                "role": r.get("role") or info.get("role"),
                "kind": r.get("kind", ""),  # "agent" | "integration" | …
                "files": summary.get("files", []),
                "lean": summary.get("lean", {}),
                "blueprint": summary.get("blueprint", {}),
                "sorry_delta": summary.get("sorry_delta", 0),
                "other_count": summary.get("other_count", 0),
            })
        return {"run": run_id, "session": session, "commits": commits_out}

    def session_file_diff(
        self, run_id: str, session: str, path: str, *, worktree: bool = False, sha: str | None = None
    ) -> dict[str, Any]:
        """Unified diff of one changed file vs the session commit's git parent —
        or, with ``worktree``, the current working tree vs the last committed
        session (the live view for a running session). With ``sha``, the diff is
        scoped to that ONE commit (vs its own parent) for the commit-granular view."""
        from archon_horizon.server.changes_api import session_file_diff
        from archon_horizon.vcs.git import WorkspaceGit, git_available

        if sha:
            # Commit-granular: exactly what this one commit changed to the file.
            return session_file_diff(self.root, sha, path, base=None)
        integrated = self._session_integrations(run_id)
        if worktree:
            last_commit = self._last_run_commit(run_id)
            return session_file_diff(self.root, None, path, base=last_commit, worktree=True)
        # Span the same commit set used by ``run_changes``. For agent sessions,
        # that means agent-authored semantic commits only; deterministic
        # integration sweeps are not allowed to add unrelated files to the diff.
        wsgit = WorkspaceGit(self.root) if git_available() else None
        info = integrated.get(session) or {}
        role = str(info.get("role") or "").lower()
        rows = wsgit.session_commits_detailed(run_id, session) if wsgit is not None else []
        if role in ("ground", "horizon"):
            rows = [r for r in rows if r.get("kind") == "agent"]
        else:
            deterministic = [r for r in rows if r.get("kind") != "agent"]
            rows = deterministic or rows
        if rows:
            first_sha, last_sha = rows[0]["sha"], rows[-1]["sha"]
            return session_file_diff(self.root, last_sha, path, base=wsgit.parent_sha(first_sha))
        if role not in ("ground", "horizon"):
            target_commit = info.get("workspace_commit")
            return session_file_diff(self.root, target_commit, path, base=None)
        return {"path": path, "available": False, "diff": ""}

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
        # Child sessions are materialized on the write path (harness end) and,
        # for a live session, by the state poll — no need to re-derive here.
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
        """The session's report artifacts, sitting next to its transcript ref."""
        path = (self.root / ref).resolve()
        if self.root.resolve() not in path.parents:  # contain path traversal
            raise ValueError("report ref escapes the workspace")
        report_path = path.parent / "report.md"
        recommendation_path = path.parent / "recommendation.md"
        return {
            "markdown": report_path.read_text("utf-8") if report_path.is_file() else "",
            "recommendation": (
                recommendation_path.read_text("utf-8")
                if recommendation_path.is_file()
                else ""
            ),
        }

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
        # One change-view per run, so the static export precomputes each run's
        # per-session diff/sorry summary (the live server computes it on demand),
        # plus one file-diff endpoint per changed Lean/blueprint file so the
        # click-to-diff works on the static page too.
        for run_id in self.stores.run_logs.ids():
            eps.append(f"/api/run/changes?run={run_id}")
            try:
                changes = self.run_changes(run_id)
            except Exception:
                continue
            for session in changes.get("sessions", []):
                # Commit-granular view for this session (message + per-commit files).
                eps.append(f"/api/session/commits?run={run_id}&session={session['session']}")
                for f in session.get("files", []):
                    if f.get("category") in ("lean", "blueprint"):
                        eps.append(
                            f"/api/session/file-diff?run={run_id}&session={session['session']}&path={f['path']}"
                        )
                # Per-commit file diffs, so click-to-diff on a single commit works offline too.
                try:
                    for c in self.session_commits_view(run_id, session["session"]).get("commits", []):
                        for f in c.get("files", []):
                            if f.get("category") in ("lean", "blueprint"):
                                eps.append(
                                    f"/api/session/file-diff?run={run_id}&session={session['session']}"
                                    f"&path={f['path']}&sha={c['sha']}"
                                )
                except Exception:
                    pass
        for name in self._discover_projects():
            encoded_name = quote(name, safe='')
            eps.append(f"/api/blueprint/chapters?project={encoded_name}")
            # Full per-project DAG (heavy) — the Blueprint/DAG pages fetch this on
            # demand now that /api/state carries only light DAG nodes, so the
            # static export must precompute it too or those pages lose node
            # statements / proofs / Lean source.
            eps.append(f"/api/blueprint/dag?project={encoded_name}")
            eps.append(f"/api/source?project={encoded_name}")
            eps.append(f"/api/project/history?project={encoded_name}")
            try:
                for f in list_lean_files(self._project_path(name)):
                    eps.append(
                        f"/api/source/file?project={encoded_name}&path={quote(f['path'], safe='')}"
                    )
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
        if parsed.path == "/api/run/changes":
            return self.run_changes((query.get("run") or [""])[0])
        if parsed.path == "/api/run/working-changes":
            return self.working_changes(
                (query.get("run") or [""])[0],
                (query.get("session") or [""])[0] or None,
            )
        if parsed.path == "/api/session/commits":
            return self.session_commits_view(
                (query.get("run") or [""])[0],
                (query.get("session") or [""])[0],
            )
        if parsed.path == "/api/session/file-diff":
            return self.session_file_diff(
                (query.get("run") or [""])[0],
                (query.get("session") or [""])[0],
                (query.get("path") or [""])[0],
                worktree=(query.get("worktree") or [""])[0] in ("1", "true"),
                sha=(query.get("sha") or [""])[0] or None,
            )
        if parsed.path == "/api/projects":
            return self.projects_summary()
        if parsed.path == "/api/project/history":
            proj = (query.get("project") or [""])[0]
            try:
                limit = int((query.get("limit") or [str(_PROJECT_HISTORY_LIMIT)])[0])
            except ValueError:
                limit = _PROJECT_HISTORY_LIMIT
            if proj:
                return {"project": proj, "limit": max(1, min(_PROJECT_HISTORY_MAX_LIMIT, limit)), "history": self._project_history(proj, limit=limit)}
            return {"project": "", "history": []}
        if parsed.path == "/api/blueprint/chapters":
            proj = (query.get("project") or [""])[0]
            if proj:
                return project_chapters(self.workspace, proj)
            return project_chapters(self.workspace, next(iter(self._discover_projects()), ""))
        if parsed.path == "/api/blueprint/dag":
            # Full (heavy) single-project DAG, fetched on demand by the Blueprint
            # and DAG pages. Kept out of /api/state so the poll stays small.
            proj = (query.get("project") or [""])[0]
            return published_dag(self.workspace, proj) or {}
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

    def sync_blueprint_dags(self) -> dict[str, Any]:
        """Refresh the published rich blueprint DAG cache for the live dashboard."""
        dags = workspace_dags_rich(self.workspace)
        out_dir = self.workspace.state_path / "blueprints"
        out_dir.mkdir(parents=True, exist_ok=True)
        projects: list[dict[str, Any]] = []
        for project, dag in dags.items():
            path = out_dir / f"{project}.json"
            path.write_text(json.dumps(dag, indent=2, sort_keys=True), "utf-8")
            projects.append({
                "project": project,
                "nodes": len(dag.get("nodes", ())),
                "edges": len(dag.get("edges", ())),
                "path": path.relative_to(self.workspace.root).as_posix(),
            })
        return {"ok": True, "projects": projects}

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
            # Soft-delete: keep the record but hide it from the default view.
            provider.update_status(item_id, InboxStatus.ARCHIVED, actor)
        elif action == "unarchive":
            provider.update_status(item_id, InboxStatus.OPEN, actor)
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
                metadata=apply_hierarchy(
                    {
                        "author": author,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                    depth=kw.get("depth"),
                    parent=kw.get("parent"),
                ),
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
                    metadata=apply_hierarchy(
                        {
                            **old.metadata,
                            "author": kw.get("author", old.metadata.get("author", "unknown")),
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                        depth=kw.get("depth"),
                        parent=kw.get("parent"),
                    ),
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
            new_status = TaskStatus(kw["status"])
            self.stores.tasks.put(dataclasses.replace(old, status=new_status, updated_at=datetime.now(timezone.utc)))
            if old.status != new_status:
                self._record_history(self.stores.tasks, task_id, actor, "status",
                                     before=old.status.value, after=new_status.value)
                self._sync_task_roadmap_refs(old, new_status, actor)
            return {"ok": True, "id": task_id}
        if action != "edit":
            raise ValueError(f"unknown task action {action!r}")

        projects = tuple(kw.get("projects", old.projects or ((old.project,) if old.project else ())))
        project = projects[0] if projects else old.project
        self.stores.tasks.put(self._task_from_payload(task_id, project, kw, old=old))
        self._record_history(self.stores.tasks, task_id, actor, "edited", note="fields updated")
        return {"ok": True, "id": task_id}

    def _sync_task_roadmap_refs(self, task: HorizonTask, status: TaskStatus, actor: str) -> None:
        target = roadmap_status_for_task_status(status)
        if target is None or not task.roadmap_refs:
            return
        roadmap = self.stores.roadmap.load()
        changed = False
        items: list[RoadmapItem] = []
        wanted = set(task.roadmap_refs)
        for item in roadmap.items:
            if item.id not in wanted or item.status == target:
                items.append(item)
                continue
            self._record_history(
                self.stores.roadmap,
                item.id,
                actor,
                "status",
                before=item.status.value,
                after=target.value,
                note=f"synced from task {task.id}",
            )
            self.stores.roadmap.add_comment(
                item.id,
                f"**Status synced from task `{task.id}`.**\n\n"
                f"- Task status changed to `{status.value}`.\n"
                f"- Roadmap item moved from `{item.status.value}` to `{target.value}`.",
                actor,
            )
            items.append(dataclasses.replace(
                item,
                status=target,
                metadata={**item.metadata, "updated_at": datetime.now(timezone.utc).isoformat()},
            ))
            changed = True
        if changed:
            self.stores.roadmap.save(Roadmap(version=roadmap.version, items=tuple(items)))

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
