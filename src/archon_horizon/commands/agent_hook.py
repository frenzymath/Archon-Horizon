"""Model-visible inbox checkpoints for Claude Code and Codex sessions.

The normal CLI synchronizer is deliberately stderr-only, which keeps JSON clean
but also means an agent can discard it with ``2>/dev/null``.  Horizon-launched
engines call this hidden command from lifecycle hooks instead.  It returns the
engines' shared ``additionalContext`` shape so protections and direct
conversations enter the model context at a tool boundary.

Hot path (every tool boundary) must stay cheap: engines spawn a fresh process
per event, so this module keeps imports minimal and short-circuits when the
inbox stamp, parallel-run stamp, and dirty/reminder timers have not moved.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

# Keep module-level imports to the stdlib. Typer and the rest of the Horizon
# CLI stack are only needed for the hidden ``horizon agent-hook`` command path;
# the engine hook command uses :func:`main_fast` instead.

_DEFAULT_REMINDER_EVERY = 40
_COMMIT_REMINDER_EVERY = 40
_COMMIT_REMINDER_SECONDS = 20 * 60
_PROGRESS_REMINDER_EVERY = 120
_PROGRESS_REMINDER_SECONDS = 60 * 60
_ITEM_LIMIT = 3
_SNIPPET_LIMIT = 320
_HOOK_STATE_VERSION = 3
# Parallel-run probes walk run dirs and optionally re-stat session metas; only
# recheck on a cadence (or when the runs directory mtime moves).
_PARALLEL_CHECK_SECONDS = 30.0
# Default on-disk state directory; almost every workspace uses this, and hooks
# can resolve paths without parsing config.yaml when the env/default is enough.
_DEFAULT_STATE_DIR = ".archon-horizon"
_COMMIT_COMMAND = re.compile(
    r'(?:^|[;&|]\s*)(?:git|hgit|\$HORIZON_GIT|"\$HORIZON_GIT")'
    r"(?:\s+-\S+)*\s+commit(?:\s|$)"
)
_MUTATING_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:apply_patch|sed\s+-i|perl\s+-pi|git\s+apply|"
    r"(?:cat|tee|printf|echo)\b[^\n]*(?:>|>>)|touch\s|cp\s|mv\s|rm\s|mkdir\s)"
)
_HORIZON_STATE_COMMAND = re.compile(
    r'(?:^|[;&|]\s*)(?:"?\$HORIZON_BIN"?|(?:[^\s;&|]*/)?horizon)'
    r'(?:\s+--root(?:=\S+|\s+\S+))?\s+'
    r'(?:task\s+(?:add|set|comment|remove)|'
    r'roadmap\s+(?:add|set|comment|remove)|'
    r'inbox\s+(?:add|dm|comment|protect|edit|edit-comment|label|complete|archive|'
    r'reject|read|unread|own|delete)|'
    r'project\s+(?:add|remove)|freeze\s+(?:add|remove)|skills\s+install|'
    r'graph(?:\s+[^\s;&|]+){0,6}\s+(?:add\s+(?:comment|review)|modify\s+node|sync))\b'
)
_MUTATING_TOOLS = {"edit", "write", "notebookedit", "apply_patch", "applypatch"}
_REPORT_SECTION = re.compile(
    r"(?im)^##\s+(?:progress|issues|why i stopped|next)\s*$"
)
_MIN_REPORT_LENGTH = 40


def _state_dir_name(root: Path) -> str:
    """State-dir segment without forcing a full config parse when possible."""
    override = os.environ.get("ARCHON_HORIZON_STATE_DIR", "").strip()
    if override:
        return override
    default_path = root / _DEFAULT_STATE_DIR
    if default_path.is_dir():
        return _DEFAULT_STATE_DIR
    try:
        from archon_horizon.config.loader import load_config

        return load_config(root).state_dir
    except Exception:
        return _DEFAULT_STATE_DIR


def _provenance_task() -> str | None:
    return os.environ.get("ARCHON_HORIZON_TASK", "").strip() or None


def _provenance_project() -> str | None:
    raw = os.environ.get("ARCHON_HORIZON_PROJECTS", "").strip()
    if not raw:
        return None
    first = raw.replace(",", " ").split()
    return first[0] if first else None


def _reader_id() -> str:
    for var in ("ARCHON_HORIZON_TASK", "ARCHON_HORIZON_RUN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower()
    return role or "human"


def _attention_items(root: Path) -> tuple[list[Any], list[Any], list[Any]]:
    """Return protections plus unread direct and advisory items for this team."""
    from archon_horizon.core.inbox import (
        InboxFilter,
        InboxKind,
        InboxStatus,
        is_conversation,
        is_read_by,
        reaches_horizon,
    )
    from archon_horizon.core.labels import is_agent_ready
    from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

    state_dir = _state_dir_name(root)
    provider = FilesystemInboxProvider(root / state_dir / "inbox" / "local")
    task = _provenance_task()
    project = _provenance_project()
    run = os.environ.get("ARCHON_HORIZON_RUN", "").strip() or None
    reader = _reader_id()
    # Open-only: archived/closed files stay unread (status peek + no history).
    candidates = provider.list_items(
        InboxFilter(status=InboxStatus.OPEN, owner_task=task)
    )
    inbox_refs: tuple[str, ...] = ()
    if task:
        try:
            from archon_horizon.commands.shared import task_inbox_refs
            from archon_horizon.config.loader import build_workspace, load_config

            cfg = load_config(root)
            inbox_refs = task_inbox_refs(build_workspace(cfg, root), task)
        except Exception:
            inbox_refs = ()
    relevant = [
        item
        for item in candidates
        if is_agent_ready(item.labels)
        and reaches_horizon(
            item,
            project,
            task=task,
            run=run,
            inbox_refs=inbox_refs,
        )
    ]
    protections = sorted(
        (item for item in relevant if item.kind is InboxKind.PROTECTION),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    conversations = sorted(
        (
            item
            for item in relevant
            if is_conversation(item) and not is_read_by(item, reader)
        ),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    notifications = sorted(
        (
            item
            for item in relevant
            if item.kind is not InboxKind.PROTECTION
            and not is_conversation(item)
            and not is_read_by(item, reader)
        ),
        key=lambda item: item.updated_at,
        reverse=True,
    )
    return protections, conversations, notifications


def _compact(text: str, limit: int = _SNIPPET_LIMIT) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _item_line(item: Any) -> str:
    author = str(item.author or "unknown").strip()
    body = _compact(item.body)
    comments = [
        comment
        for comment in item.metadata.get("comments", [])
        if isinstance(comment, dict) and str(comment.get("body") or "").strip()
    ]
    latest = ""
    if comments:
        comment = comments[-1]
        latest_author = str(comment.get("author") or "unknown").strip()
        latest = f' Latest reply from {latest_author}: "{_compact(comment.get("body"), 320)}"'
    return f'- {item.id} from {author}: "{body}"{latest}'


def _context(
    protections: list[Any],
    conversations: list[Any],
    notifications: list[Any],
    *,
    reminder: bool,
) -> str:
    sections: list[str] = []
    # Standing protections enter at session/subagent start and whenever their
    # files change. Replaying unchanged bodies periodically only makes the model
    # reread the same constraints; compact/resume fires SessionStart again.
    if protections and not reminder:
        rows = "\n".join(_item_line(item) for item in protections[:_ITEM_LIMIT])
        more = len(protections) - _ITEM_LIMIT
        if more > 0:
            rows += f"\n- +{more} more active protection(s)"
        sections.append(
            "HORIZON REQUIRED PROTECTIONS (binding constraints, not advisory):\n"
            f"{rows}\n"
            "Consult the full items before editing affected code: "
            '`"$HORIZON_BIN" inbox list --mine --status open --kind protection --json`.'
        )
    if conversations:
        rows = "\n".join(_item_line(item) for item in conversations[:_ITEM_LIMIT])
        more = len(conversations) - _ITEM_LIMIT
        if more > 0:
            rows += f"\n- +{more} more unread conversation(s)"
        lead = "HORIZON REMINDER" if reminder else "HORIZON NEW DIRECT CONVERSATION"
        first = conversations[0].id
        sections.append(
            f"{lead} (live team message; higher priority than advisory inbox):\n"
            f"{rows}\n"
            "Open and acknowledge the thread before continuing normal work: "
            f'`"$HORIZON_BIN" inbox show {first} --json`; reply with '
            f'`"$HORIZON_BIN" inbox comment {first} --body "Reply"`. '
            "Opening it marks it read for this team; replies make it unread again."
        )
    if notifications and not reminder:
        rows = "\n".join(_item_line(item) for item in notifications[:_ITEM_LIMIT])
        more = len(notifications) - _ITEM_LIMIT
        if more > 0:
            rows += f"\n- +{more} more unread notification(s)"
        first = notifications[0].id
        sections.append(
            "HORIZON NEW INBOX NOTIFICATION (unread advisory item):\n"
            f"{rows}\n"
            "Review when relevant: "
            f'`"$HORIZON_BIN" inbox show {first} --json`; all unread: '
            '`"$HORIZON_BIN" inbox list --mine --unread --json`.'
        )
    return "\n\n".join(sections)


def _signature(
    protections: list[Any], conversations: list[Any], notifications: list[Any]
) -> str:
    rows = [
        ("protection", item.id, item.updated_at.isoformat())
        for item in protections
    ] + [
        ("conversation", item.id, item.updated_at.isoformat())
        for item in conversations
    ] + [
        ("notification", item.id, item.updated_at.isoformat())
        for item in notifications
    ]
    return hashlib.sha256(
        json.dumps(rows, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _parallel_runs(root: Path) -> list[dict[str, str]]:
    """Stable identities for other live runs, excluding volatile PID/activity data."""
    from archon_horizon.commands.ps import live_runs

    try:
        mine = os.environ.get("ARCHON_HORIZON_RUN", "").strip() or None
        live = live_runs(
            root / _state_dir_name(root) / "runs",
            exclude_run=mine,
            reap=False,
        )
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for run in live:
        rows.append({
            "run": str(run.get("run") or ""),
            "task": str(run.get("task") or ""),
            "title": _compact(str(run.get("task_title") or ""), 96),
        })
    return rows


def _runs_dir_stamp(root: Path) -> str:
    """Cheap mtime/size probe of the runs directory (and process markers)."""
    try:
        runs_dir = root / _state_dir_name(root) / "runs"
        if not runs_dir.is_dir():
            return "0"
        dir_mtime = runs_dir.stat().st_mtime_ns
        count = 0
        max_mtime = dir_mtime
        with os.scandir(runs_dir) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                count += 1
                marker = Path(entry.path) / "process.json"
                try:
                    st = marker.stat()
                except OSError:
                    continue
                if st.st_mtime_ns > max_mtime:
                    max_mtime = st.st_mtime_ns
        return f"{count}:{dir_mtime}:{max_mtime}"
    except Exception:
        return "0"


def _run_line(run: dict[str, str]) -> str:
    label = f"run {run['run']}"
    if run.get("task"):
        label += f" · task {run['task']}"
    if run.get("title"):
        label += f' "{run["title"]}"'
    return label


def _parallel_context(
    previous: list[dict[str, str]], current: list[dict[str, str]], *, initial: bool
) -> str:
    """Describe the current parallel set or a concise transition since last hook."""
    current_by_id = {run["run"]: run for run in current}
    previous_by_id = {run["run"]: run for run in previous}
    lines: list[str] = []
    if initial:
        lines.extend(f"- {_run_line(run)}" for run in current[:4])
        heading = "HORIZON PARALLEL RUNS (sharing this workspace):"
    else:
        for run_id in sorted(current_by_id.keys() - previous_by_id.keys()):
            lines.append(f"- started: {_run_line(current_by_id[run_id])}")
        for run_id in sorted(previous_by_id.keys() - current_by_id.keys()):
            lines.append(f"- stopped: {_run_line(previous_by_id[run_id])}")
        for run_id in sorted(current_by_id.keys() & previous_by_id.keys()):
            if current_by_id[run_id] != previous_by_id[run_id]:
                lines.append(f"- changed: {_run_line(current_by_id[run_id])}")
        heading = "HORIZON PARALLEL RUNS CHANGED:"
    if len(current) > 4:
        lines.append(f"- +{len(current) - 4} more active run(s)")
    tail: list[str] = []
    if not initial:
        tail.append(f"Active now: {len(current)} run(s).")
    if current:
        recipient = next(
            (f"task:{run['task']}" for run in current if run.get("task")),
            f"run:{current[0]['run']}",
        )
        tail.append(
            "Coordinate with "
            f'`"$HORIZON_BIN" inbox dm {recipient} --body "Title\\n\\nMessage"`.'
        )
    tail.append('Details: `"$HORIZON_BIN" ps --json`.')
    return f"{heading}\n" + "\n".join([*lines, *tail])


def _inbox_stamp(root: Path) -> str | None:
    """Cheap change detector so the normal per-tool path never parses the inbox.

    Content edits do not always bump the items directory mtime, so the stamp
    still samples each item file's mtime/size. Comments live beside items and
    affect attention snippets, so their tree is folded in cheaply via the
    comments directory mtime when present.
    """
    try:
        state_dir = _state_dir_name(root)
        local = root / state_dir / "inbox" / "local"
        items_dir = local / "items"
        if not items_dir.is_dir():
            return "0:0:0:0"
        count = 0
        max_mtime = 0
        total_size = 0
        with os.scandir(items_dir) as entries:
            for entry in entries:
                if not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                count += 1
                if stat.st_mtime_ns > max_mtime:
                    max_mtime = stat.st_mtime_ns
                total_size += stat.st_size
        comments_mtime = 0
        comments_dir = local / "comments"
        if comments_dir.is_dir():
            try:
                comments_mtime = comments_dir.stat().st_mtime_ns
            except OSError:
                comments_mtime = 0
        return f"{count}:{max_mtime}:{total_size}:{comments_mtime}"
    except Exception:
        return None


def _state_path(root: Path, payload: dict[str, Any]) -> Path | None:
    session_dir = os.environ.get("ARCHON_HORIZON_SESSION_DIR", "").strip()
    if session_dir:
        return Path(session_dir) / "inbox-hook-state.json"
    session_id = str(payload.get("session_id") or "").strip()
    if session_id:
        safe_id = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")[:96]
        return root / ".archon-horizon" / "cache" / "hooks" / f"{safe_id}.json"
    return None


@contextmanager
def _locked_state(path: Path | None) -> Iterator[dict[str, Any]]:
    if path is None:
        yield {}
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        try:
            state = json.loads(path.read_text("utf-8"))
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        before = json.dumps(state, sort_keys=True, separators=(",", ":"))
        yield state
        after = json.dumps(state, sort_keys=True, separators=(",", ":"))
        if after == before:
            return
        temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        try:
            temporary.write_text(after, "utf-8")
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)


def _reminder_every() -> int:
    try:
        return max(
            1,
            int(
                os.environ.get(
                    "ARCHON_HORIZON_INBOX_REMINDER_EVERY",
                    str(_DEFAULT_REMINDER_EVERY),
                )
            ),
        )
    except ValueError:
        return _DEFAULT_REMINDER_EVERY


def _is_commit_tool(payload: dict[str, Any]) -> bool:
    if str(payload.get("tool_name") or "").lower() not in {
        "bash", "shell", "exec_command", "functions.exec_command",
    }:
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command") or tool_input.get("cmd")
    return isinstance(command, str) and bool(_COMMIT_COMMAND.search(command))


def _is_mutating_tool(payload: dict[str, Any]) -> bool:
    name = str(payload.get("tool_name") or "").lower()
    if name in _MUTATING_TOOLS or name.endswith("apply_patch"):
        return True
    if name not in {"bash", "shell", "exec_command", "functions.exec_command"}:
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    command = tool_input.get("command") or tool_input.get("cmd")
    return isinstance(command, str) and bool(
        _MUTATING_COMMAND.search(command) or _HORIZON_STATE_COMMAND.search(command)
    )


def _tool_succeeded(payload: dict[str, Any]) -> bool:
    """Treat absent engine result metadata as success for backward compatibility."""
    for key in ("tool_response", "tool_result", "result"):
        result = payload.get(key)
        if not isinstance(result, dict):
            continue
        for code_key in ("exit_code", "exitCode", "returncode"):
            if code_key in result:
                try:
                    return int(result[code_key]) == 0
                except (TypeError, ValueError):
                    return False
        if "is_error" in result:
            return not bool(result["is_error"])
    return True


def _seconds_setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _has_final_report(payload: dict[str, Any]) -> bool:
    """Whether Stop carries the structured hand-off saved as ``report.md``."""
    message = str(payload.get("last_assistant_message") or "").strip()
    return len(message) >= _MIN_REPORT_LENGTH and bool(_REPORT_SECTION.search(message))


def _runtime_status_path(root: Path, path: str) -> bool:
    """Exclude live session artifacts that the orchestrator owns at finalization."""
    session_dir = os.environ.get("ARCHON_HORIZON_SESSION_DIR", "").strip()
    if session_dir:
        try:
            relative = Path(session_dir).resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            relative = ""
        if relative and (path == relative or path.startswith(relative + "/")):
            return True
    run = os.environ.get("ARCHON_HORIZON_RUN", "").strip()
    return bool(run and path == f".archon-horizon/runs/{run}/process.json")


def _non_actionable_ledger_path(path: str) -> bool:
    """Paths agents should not treat as their own commit debt.

    The agent ledger is source-only (Lean, blueprints, ``config.yaml``). Horizon
    state (``.archon-horizon/``) and entire ``hgraph/`` trees are excluded from
    new commits and residual tracked copies are not actionable until pruned via
    ``horizon ledger prune``. Concurrent-run process markers are likewise noise.
    """
    norm = path.replace("\\", "/")
    if norm == ".archon-horizon" or norm.startswith(".archon-horizon/"):
        return True
    if "/hgraph/" in f"/{norm}/" or norm == "hgraph" or norm.endswith("/hgraph"):
        return True
    if norm.endswith("/process.json") and "/runs/" in norm:
        return True
    return False


def _ledger_dirty_paths(root: Path) -> tuple[str, ...] | None:
    """Return durable uncommitted ledger paths, or ``None`` if status is unavailable.

    Uses the shared single-flight porcelain helper with ``untracked=no`` so a
    large worktree (references dumps, site builds) cannot stall every hook and
    so the commit checkpoint only surfaces *tracked* drift an agent can fix.
    """
    git_dir = os.environ.get("HORIZON_LEDGER_GIT_DIR", "").strip()
    work_tree = os.environ.get("HORIZON_LEDGER_WORK_TREE", "").strip() or str(root)
    if not git_dir:
        # Fall back to the conventional out-of-tree ledger location.
        candidate = Path(work_tree) / ".archon-horizon" / "vcs" / "workspace.git"
        if candidate.is_dir():
            git_dir = str(candidate)
        else:
            return None
    try:
        from archon_horizon.vcs.git import status_porcelain

        text = status_porcelain(
            Path(git_dir),
            Path(work_tree),
            untracked="no",
        )
    except Exception:
        return None
    paths: list[str] = []
    for line in text.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        path = path.strip('"')
        if (
            path
            and not _runtime_status_path(root, path)
            and not _non_actionable_ledger_path(path)
        ):
            paths.append(path)
    return tuple(paths)


def _commit_checkpoint(
    root: Path,
    state: dict[str, Any],
    parallel_runs: list[dict[str, str]],
) -> tuple[bool, tuple[str, ...]]:
    """Prefer real ledger status unless another live run makes it ambiguous."""
    if not parallel_runs:
        dirty_paths = _ledger_dirty_paths(root)
        if dirty_paths is not None:
            return bool(dirty_paths), dirty_paths
    return state.get("dirty_since_call") is not None, ()


def _resolve_parallel(
    root: Path,
    state: dict[str, Any],
    *,
    force: bool,
    now: float,
) -> list[dict[str, str]]:
    """Return current parallel runs, refreshing only when the stamp or TTL says so."""
    previous = [
        row for row in state.get("parallel_runs", [])
        if isinstance(row, dict) and str(row.get("run") or "")
    ]
    runs_stamp = _runs_dir_stamp(root)
    last_check = float(state.get("parallel_checked_at") or 0)
    cached_stamp = str(state.get("runs_stamp") or "")
    ttl = _seconds_setting(
        "ARCHON_HORIZON_PARALLEL_CHECK_SECONDS",
        int(_PARALLEL_CHECK_SECONDS),
    )
    if (
        not force
        and "parallel_runs" in state
        and cached_stamp == runs_stamp
        and now - last_check < ttl
    ):
        return previous
    current = _parallel_runs(root)
    state["parallel_runs"] = current
    state["runs_stamp"] = runs_stamp
    state["parallel_checked_at"] = now
    return current


def hook_response(root: Path, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Build one Claude/Codex-compatible hook response, or stay silent."""
    event = str(payload.get("hook_event_name") or "").strip()
    if event not in {
        "SessionStart", "SubagentStart", "PreToolUse", "PostToolUse", "Stop",
    }:
        return None
    # Both engines set this after a Stop hook has already continued the turn. The
    # second Stop is always allowed, even when cleanup is incomplete, so a broken
    # commit or report command cannot wedge the session forever.
    if event == "Stop" and bool(payload.get("stop_hook_active")):
        return None
    # PreToolUse is registered on all Bash only because the engine matcher is
    # tool-name scoped. Non-commit Bash has nothing to gate — exit before any
    # inbox/state I/O so the common shell path is essentially free.
    if event == "PreToolUse" and not _is_commit_tool(payload):
        return None
    state_path = _state_path(root, payload)
    inbox_stamp = _inbox_stamp(root)

    with _locked_state(state_path) as state:
        now = time.time()
        state.setdefault("started_at", now)
        calls = int(state.get("tool_calls") or 0)
        last_notice = int(state.get("last_notice_call") or 0)
        # Delivered signature is what the model has actually seen. Never fall
        # back to the cached scan signature: PreToolUse refreshes the cache
        # without delivering, and a fallback would swallow the next PostToolUse.
        previous_signature = str(state.get("last_delivered_signature") or "")
        previous_parallel = [
            row for row in (state.get("last_delivered_parallel") or [])
            if isinstance(row, dict) and str(row.get("run") or "")
        ]
        if event == "PostToolUse":
            calls += 1
            state["tool_calls"] = calls
            if _is_commit_tool(payload) and _tool_succeeded(payload):
                state.pop("dirty_since_call", None)
                state.pop("dirty_since_at", None)
                state["last_commit_call"] = calls
                state["last_commit_at"] = now
            elif _is_mutating_tool(payload):
                state.setdefault("dirty_since_call", calls)
                state.setdefault("dirty_since_at", now)

        # Full inbox parsing is relatively expensive in mature workspaces. The
        # common PostToolUse path checks only the sharded-file stamp and reuses
        # the last model context; a write/reply/read changes the stamp and forces
        # a refresh at the very next tool boundary.
        cached = (
            state.get("version") == _HOOK_STATE_VERSION
            and
            state.get("inbox_stamp") == inbox_stamp
            and "signature" in state
            and "context" in state
        )
        if cached:
            signature = str(state.get("signature") or "")
            context = str(state.get("context") or "")
            reminder_context = str(state.get("reminder_context") or context)
            has_attention = bool(state.get("has_attention"))
            has_conversations = bool(state.get("has_conversations"))
        else:
            protections, conversations, notifications = _attention_items(root)
            signature = _signature(protections, conversations, notifications)
            context = _context(
                protections, conversations, notifications, reminder=False
            )
            reminder_context = _context(
                protections, conversations, notifications, reminder=True
            )
            has_attention = bool(protections or conversations or notifications)
            has_conversations = bool(conversations)
            state.update({
                "version": _HOOK_STATE_VERSION,
                "inbox_stamp": inbox_stamp,
                "signature": signature,
                "context": context,
                "reminder_context": reminder_context,
                "has_attention": has_attention,
                "has_conversations": has_conversations,
            })

        force_parallel = event in {"SessionStart", "SubagentStart", "Stop"}
        current_parallel = _resolve_parallel(
            root, state, force=force_parallel, now=now
        )

        if event == "Stop":
            reasons: list[str] = []
            if has_conversations:
                reasons.append(reminder_context)
            dirty, dirty_paths = _commit_checkpoint(root, state, current_parallel)
            if dirty:
                path_note = ""
                if dirty_paths:
                    shown = ", ".join(f"`{path}`" for path in dirty_paths[:8])
                    more = len(dirty_paths) - 8
                    path_note = f" Remaining ledger paths: {shown}"
                    if more > 0:
                        path_note += f", plus {more} more."
                    else:
                        path_note += "."
                reasons.append(
                    "HORIZON COMMIT CHECKPOINT: durable changes remain after the last "
                    "observed ledger commit. Commit coherent authored changes with "
                    "`$HORIZON_GIT` using explicit paths. Do not commit another writer's "
                    "changes; identify pre-existing or concurrent paths in the report "
                    f"instead.{path_note}"
                )
            interactive = os.environ.get("ARCHON_HORIZON_INTERACTIVE", "").strip() == "1"
            if not interactive and not _has_final_report(payload):
                reasons.append(
                    "HORIZON REPORT CHECKPOINT: finish with a self-contained hand-off in "
                    "your last message. Use the informative sections among `## Progress`, "
                    "`## Issues`, `## Why I stopped`, and `## Next`; state checks not run "
                    "and any remaining uncommitted or blocked work. This last message is "
                    "what Horizon saves as `report.md`."
                )
            if not reasons:
                return None
            return {
                "decision": "block",
                "reason": (
                    "HORIZON FINALIZATION CHECK (one retry before Stop):\n\n"
                    + "\n\n".join(reasons)
                ),
            }

        if event == "PreToolUse":
            # Commit gate only — general inbox injection lives on PostToolUse so
            # every Bash call does not pay for a second model-context payload.
            # The scan above still refreshes cache; delivery tracking keeps the
            # next PostToolUse able to inject a newly-seen signature.
            if has_conversations and _is_commit_tool(payload):
                state["last_notice_call"] = calls
                state["last_delivered_signature"] = signature
                return {
                    "hookSpecificOutput": {
                        "hookEventName": event,
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Commit paused for an unread Horizon conversation.\n\n"
                            + reminder_context
                        ),
                    }
                }
            return None

        # Every subagent needs its own direct context, independent of whether the
        # parent was recently reminded. SessionStart establishes the parent's
        # initial checkpoint and seeds the reminder state.
        if event in {"SessionStart", "SubagentStart"}:
            if event == "SessionStart":
                state["last_notice_call"] = calls
            notices = [context] if has_attention else []
            if current_parallel:
                notices.append(_parallel_context([], current_parallel, initial=True))
            if not notices:
                state["last_delivered_signature"] = signature
                state["last_delivered_parallel"] = current_parallel
                return None
            state["last_delivered_signature"] = signature
            state["last_delivered_parallel"] = current_parallel
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": "\n\n".join(notices),
                }
            }

        # PostToolUse (and any other residual event): inject only on change or
        # a slow reminder cadence so the common no-op path stays silent.
        changed = signature != previous_signature
        # Parallel delivery uses the last *delivered* set, not the last probe.
        parallel_notice = current_parallel != previous_parallel
        due = has_conversations and calls - last_notice >= _reminder_every()
        notices: list[str] = []
        if has_attention and (changed or due):
            notices.append(context if changed else reminder_context)
            state["last_notice_call"] = calls
            state["last_delivered_signature"] = signature
        if parallel_notice:
            notices.append(
                _parallel_context(previous_parallel, current_parallel, initial=False)
            )
            state["last_delivered_parallel"] = current_parallel
        if notices:
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": "\n\n".join(notices),
                }
            }

        dirty_since = state.get("dirty_since_call")
        dirty_since_at = state.get("dirty_since_at")
        last_checkpoint = int(state.get("last_checkpoint_notice") or 0)
        last_checkpoint_at = float(state.get("last_checkpoint_at") or 0)
        commit_every = _COMMIT_REMINDER_EVERY
        commit_seconds = _seconds_setting(
            "ARCHON_HORIZON_COMMIT_REMINDER_SECONDS",
            _COMMIT_REMINDER_SECONDS,
        )
        checkpoint_due = (
            event == "PostToolUse"
            and isinstance(dirty_since, int)
            and (
                calls - dirty_since >= commit_every
                or (
                    isinstance(dirty_since_at, (int, float))
                    and now - float(dirty_since_at) >= commit_seconds
                )
            )
            and (
                not last_checkpoint
                or calls - last_checkpoint >= commit_every
            )
            and (
                not last_checkpoint_at
                or now - last_checkpoint_at >= commit_seconds
            )
        )
        if checkpoint_due:
            state["last_checkpoint_notice"] = calls
            state["last_checkpoint_at"] = now
            return {
                "hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": (
                        "HORIZON COMMIT CHECKPOINT: coherent edits are still uncommitted "
                        "(including task, roadmap, or inbox state). Checkpoint explicit "
                        "paths with `$HORIZON_GIT` "
                        "before expanding scope. Preserve rejected drafts first with "
                        "`$HORIZON_BIN attempt save ... --reason ...`."
                    ),
                }
            }
        started_at = float(state.get("started_at") or now)
        last_progress_call = int(state.get("last_progress_notice") or 0)
        last_progress_at = float(state.get("last_progress_notice_at") or 0)
        progress_every = _PROGRESS_REMINDER_EVERY
        progress_seconds = _seconds_setting(
            "ARCHON_HORIZON_PROGRESS_REMINDER_SECONDS",
            _PROGRESS_REMINDER_SECONDS,
        )
        progress_due = (
            event == "PostToolUse"
            and calls >= progress_every
            and now - started_at >= progress_seconds
            and calls - last_progress_call >= progress_every
            and (
                not last_progress_at
                or now - last_progress_at >= progress_seconds
            )
        )
        if not progress_due:
            return None
        state["last_progress_notice"] = calls
        state["last_progress_notice_at"] = now
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": (
                    "HORIZON PROGRESS CHECKPOINT: this session has been active for a long "
                    "interval. Reassess the bounded objective now: commit a coherent result, "
                    "preserve and report a failed attempt, or record the concrete blocker "
                    "before continuing exploratory work."
                ),
            }
        }


def _resolve_root() -> Path:
    """Workspace root without the full Typer CLI callback."""
    argv = sys.argv[1:]
    for index, arg in enumerate(argv):
        if arg == "--root" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if arg.startswith("--root="):
            return Path(arg.split("=", 1)[1])
    env_root = os.environ.get("ARCHON_HORIZON_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    return Path(".")


def main_fast(argv: list[str] | None = None) -> int:
    """Lightweight process entry used by engine lifecycle hooks.

    Avoids importing the full Typer CLI (and the pre-command synchronizer) so a
    no-op PostToolUse stays in the tens of milliseconds instead of hundreds.
    """
    del argv  # root comes from env / optional --root already on sys.argv
    try:
        raw = json.load(sys.stdin)
        payload = raw if isinstance(raw, dict) else {}
        response = hook_response(_resolve_root(), payload)
    except Exception:
        # Hook failures must never prevent the engine from using a tool.
        return 0
    if response is not None:
        json.dump(response, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
    return 0


def agent_hook(ctx: Any) -> None:
    """Internal lifecycle hook used by Horizon-launched agent engines.

    Prefer :func:`main_fast` from the engine command line; this Typer entry
    remains for manual debugging via ``horizon agent-hook``.
    """
    root = (
        Path(ctx.obj["root"])
        if ctx is not None and getattr(ctx, "obj", None)
        else _resolve_root()
    )
    try:
        raw = json.load(sys.stdin)
        payload = raw if isinstance(raw, dict) else {}
        response = hook_response(root, payload)
    except Exception:
        return
    if response is not None:
        json.dump(response, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
