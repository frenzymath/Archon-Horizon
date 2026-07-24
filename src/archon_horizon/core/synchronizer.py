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


def _inbox_line(root: Path) -> str | None:
    """Count of unread items in my task's inbox (owned-by-me plus shared)."""
    try:
        from archon_horizon.commands.shared import provenance_task, reader_id
        from archon_horizon.config.loader import load_config
        from archon_horizon.core.inbox import InboxFilter, InboxStatus
        from archon_horizon.inboxes.filesystem import FilesystemInboxProvider

        cfg = load_config(root)
        inbox = FilesystemInboxProvider(root / cfg.state_dir / "inbox" / "local")
        unread = inbox.list_items(
            InboxFilter(
                status=InboxStatus.OPEN,
                owner_task=provenance_task(),
                unread_for=reader_id(),
            )
        )
    except Exception:
        return None
    if not unread:
        return None
    n = len(unread)
    ids = ", ".join(item.id for item in unread[:5])
    more = f", +{n - 5} more" if n > 5 else ""
    return f"{n} unread inbox item{'s' if n != 1 else ''} ({ids}{more}) — `horizon inbox list --unread`"


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
    labels = ", ".join(r["run"] for r in others[:6])
    return f"{len(others)} other run{'s' if len(others) != 1 else ''} live on this workspace ({labels}) — `horizon ps`"


def _compute_lines(root: Path) -> list[str]:
    return [line for line in (_session_line(), _inbox_line(root), _runs_line(root)) if line]


def _cache_path() -> Path | None:
    session_dir = os.environ.get("ARCHON_HORIZON_SESSION_DIR", "").strip()
    return Path(session_dir) / "notify_cache.json" if session_dir else None


def _cached_lines(root: Path) -> list[str]:
    """Compute the digest, reusing a fresh (< TTL) cache within a session."""
    cache = _cache_path()
    if cache is not None:
        try:
            blob = json.loads(cache.read_text("utf-8"))
            if time.time() - float(blob.get("at") or 0) < _CACHE_TTL_S:
                return [str(x) for x in blob.get("lines", [])]
        except (OSError, ValueError):
            pass
    lines = _compute_lines(root)
    if cache is not None:
        try:
            cache.write_text(json.dumps({"at": time.time(), "lines": lines}), "utf-8")
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
