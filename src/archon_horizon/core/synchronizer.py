"""Pre-command notification preamble — the "synchronizer".

Runs at the start of every ``horizon`` CLI invocation (from the app callback)
and writes a short, cached digest to **stderr** so an agent is immediately aware
of anything it should react to before doing its work:

* unread items in its task's inbox (and shared items it has not read),
* how long / how many tokens the current session has run — a nudge to compact or
  wrap up a session that has grown large,
* whether other runs are live on the same workspace (parallel teams to coordinate
  with, or a stale run to reap).

It only ever writes to stderr, so ``--json`` stdout stays clean, and it is fully
best-effort: any failure is swallowed so it can never break a command. Skipped
with ``--no-sync`` or ``ARCHON_HORIZON_NO_SYNC=1``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# A short TTL so rapid successive commands in one session don't recompute the
# scan every time; the digest is a heads-up, not a live meter.
_CACHE_TTL_S = 10.0


def _fmt_duration(seconds: float) -> str:
    seconds = int(max(0.0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _session_line() -> str | None:
    """Runtime + cumulative tokens for the current session, from usage.json.

    Note: usage.json tracks *cumulative* tokens, not live context-window
    occupancy (the harness does not expose that yet), so we report elapsed time
    and tokens-out honestly rather than a context gauge.
    """
    session_dir = os.environ.get("ARCHON_HORIZON_SESSION_DIR", "").strip()
    if not session_dir:
        return None
    usage_path = Path(session_dir) / "usage.json"
    try:
        data = json.loads(usage_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    started = data.get("started_at")
    tokens_out = int(data.get("tokens_out") or 0)
    bits: list[str] = []
    if isinstance(started, (int, float)):
        bits.append(f"running {_fmt_duration(time.time() - started)}")
    if tokens_out:
        bits.append(f"{_fmt_tokens(tokens_out)} tokens out")
    if not bits:
        return None
    line = "session " + ", ".join(bits)
    # A soft nudge once the session is large; thresholds are deliberately loose.
    if tokens_out > 400_000:
        line += " — consider compacting or wrapping up this session"
    return line


def _inbox_lines(root: Path) -> list[str]:
    """Priority lanes for protections, conversations, then advisory items."""
    try:
        from archon_horizon.commands.shared import (
            provenance_project,
            provenance_task,
            reader_id,
            task_inbox_refs,
        )
        from archon_horizon.config.loader import build_workspace, load_config
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

        cfg = load_config(root)
        workspace = build_workspace(cfg, root)
        inbox = FilesystemInboxProvider(root / cfg.state_dir / "inbox" / "local")
        task = provenance_task()
        inbox_refs = task_inbox_refs(workspace, task)
        candidates = inbox.list_items(
            InboxFilter(
                status=InboxStatus.OPEN,
                owner_task=task,
            )
        )
        run = os.environ.get("ARCHON_HORIZON_RUN", "").strip() or None
        relevant = sorted(
            (
                item for item in candidates
                if is_agent_ready(item.labels) and reaches_horizon(
                    item, provenance_project(), task=task, run=run,
                    inbox_refs=inbox_refs,
                )
            ),
            key=lambda item: item.updated_at,
            reverse=True,
        )
    except Exception:
        return []

    def _title(item) -> str:
        title = str(item.body or "").split("\n", 1)[0].strip().replace('"', "'")
        return title if len(title) <= 48 else title[:45].rstrip() + "..."

    def _shown(items) -> str:
        text = "; ".join(f'{item.id} "{_title(item)}"' for item in items[:3])
        return text + (f"; +{len(items) - 3} more" if len(items) > 3 else "")

    reader = reader_id()
    protections = [item for item in relevant if item.kind is InboxKind.PROTECTION]
    conversations = [
        item for item in relevant
        if is_conversation(item) and not is_read_by(item, reader)
    ]
    advisory = [
        item for item in relevant
        if item.kind is not InboxKind.PROTECTION
        and not is_conversation(item)
        and not is_read_by(item, reader)
    ]
    lines: list[str] = []
    if protections:
        lines.append(
            f"REQUIRED · {len(protections)} active protection"
            f"{'s' if len(protections) != 1 else ''}: {_shown(protections)} — "
            "consult before editing: `horizon inbox list --mine --status open "
            "--kind protection --json`"
        )
    if conversations:
        first = conversations[0].id
        lines.append(
            f"ACTION · {len(conversations)} unread conversation"
            f"{'s' if len(conversations) != 1 else ''}: {_shown(conversations)} — "
            f"open now: `horizon inbox show {first} --json`; reply with "
            f"`horizon inbox comment {first} --body \"Reply\"`"
        )
    if advisory:
        lines.append(
            f"Advisory · {len(advisory)} unread inbox item"
            f"{'s' if len(advisory) != 1 else ''}: {_shown(advisory)} — "
            "review when relevant: `horizon inbox list --mine --unread --json`"
        )
    return lines


def _runs_line(root: Path) -> str | None:
    """Other live runs on this workspace (parallel teams / stale markers)."""
    try:
        from archon_horizon.commands.ps import live_runs
        from archon_horizon.config.loader import load_config

        cfg = load_config(root)
        mine = os.environ.get("ARCHON_HORIZON_RUN", "").strip() or None
        others = live_runs(root / cfg.state_dir / "runs", exclude_run=mine)
    except Exception:
        return None
    if not others:
        return None
    labels: list[str] = []
    recipients: list[str] = []
    for row in others[:4]:
        label = f"run {row['run']}"
        task = str(row.get("task") or "").strip()
        title = str(row.get("task_title") or "").strip()
        if title and len(title) > 42:
            title = title[:39].rstrip() + "..."
        if task:
            label += f" · task {task}"
            recipients.append(f"task:{task}")
        else:
            recipients.append(f"run:{row['run']}")
        if title:
            label += f' "{title}"'
        labels.append(label)
    recipient = recipients[0]
    return (
        f"Running sessions: {'; '.join(labels)} "
        f"— message a team with `horizon inbox dm {recipient} --body \"Title\\n\\nMessage\"`; "
        "details: `horizon ps`"
    )


def _compute_lines(root: Path) -> list[str]:
    inbox = _inbox_lines(root)
    return [*inbox, *[line for line in (_session_line(), _runs_line(root)) if line]]


def _cache_path() -> Path | None:
    session_dir = os.environ.get("ARCHON_HORIZON_SESSION_DIR", "").strip()
    return Path(session_dir) / "notify_cache.json" if session_dir else None


def _inbox_stamp(root: Path) -> str | None:
    """Cheap invalidation so a new message bypasses the digest's normal TTL."""
    try:
        from archon_horizon.config.loader import load_config

        cfg = load_config(root)
        items_dir = root / cfg.state_dir / "inbox" / "local" / "items"
        stats = [path.stat() for path in items_dir.iterdir() if path.is_file()]
        return f"{len(stats)}:{max((stat.st_mtime_ns for stat in stats), default=0)}:{sum(stat.st_size for stat in stats)}"
    except Exception:
        return None


def _cached_lines(root: Path) -> list[str]:
    """Compute the digest, reusing a fresh (< TTL) cache within a session."""
    cache = _cache_path()
    inbox_stamp = _inbox_stamp(root)
    if cache is not None:
        try:
            blob = json.loads(cache.read_text("utf-8"))
            if (
                time.time() - float(blob.get("at") or 0) < _CACHE_TTL_S
                and blob.get("inbox_stamp") == inbox_stamp
            ):
                return [str(x) for x in blob.get("lines", [])]
        except (OSError, ValueError):
            pass
    lines = _compute_lines(root)
    if cache is not None:
        try:
            cache.write_text(json.dumps({
                "at": time.time(),
                "inbox_stamp": inbox_stamp,
                "lines": lines,
            }), "utf-8")
        except OSError:
            pass
    return lines


def _in_agent_session() -> bool:
    """The synchronizer targets the agent: only run inside a run/session, so a
    human at the CLI (and the test suite) never sees the extra stderr chrome."""
    return bool(
        os.environ.get("ARCHON_HORIZON_SESSION", "").strip()
        or os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip()
    )


def synchronize(root: Path) -> None:
    """Emit the notification digest to stderr (best-effort, never raises)."""
    if not _in_agent_session():
        return
    if os.environ.get("ARCHON_HORIZON_NO_SYNC", "").strip() in {"1", "true", "yes"}:
        return
    try:
        lines = _cached_lines(root)
    except Exception:
        return
    if not lines:
        return
    from archon_horizon.log import log

    for line in lines:
        log.note_stderr(line)
