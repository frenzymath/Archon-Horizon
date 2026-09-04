"""Workspace-local scratch directories and conservative cleanup.

Agent engines and the tools they launch often use the operating system's
temporary directory for downloads, generated source, and probes.  A shared
``/tmp`` can be quota constrained or can mix unrelated runs, so Horizon gives
each invocation a disposable directory below ``.archon-horizon/tmp``.

Scratch is deliberately not a durable artifact store: reports, transcripts,
and rejected attempts belong under the run/session state tree.  The cleanup
helpers only operate below the workspace's own ``tmp`` directory and never
follow symlinks.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path

from .workspace import Workspace

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def ensure_workspace_tmp(workspace: Workspace) -> Path:
    """Create and return the workspace's shared scratch root."""

    path = workspace.state_path / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _component(value: str | None, fallback: str) -> str:
    text = _SAFE_COMPONENT.sub("-", str(value or "").strip()).strip(".-")
    return text[:100] or fallback


def session_tmp_dir(
    workspace: Workspace,
    *,
    run_id: str | None = None,
    session: str | None = None,
    role: str = "agent",
) -> Path:
    """Return (and create) an isolated scratch directory for one invocation."""

    root = ensure_workspace_tmp(workspace)
    run_part = _component(run_id, "adhoc")
    session_part = _component(session, role)
    path = root / run_part / session_part
    path.mkdir(parents=True, exist_ok=True)
    return path


def scratch_environment(
    workspace: Workspace,
    *,
    run_id: str | None = None,
    session: str | None = None,
    role: str = "agent",
) -> tuple[Path, dict[str, str]]:
    """Build environment variables directing temp-aware tools to scratch.

    ``TMPDIR`` is the POSIX convention; ``TMP`` and ``TEMP`` cover common
    cross-platform libraries.  ``ARCHON_HORIZON_TMP_ROOT`` lets a janitor find
    the workspace scope while ``ARCHON_HORIZON_TMP`` is the current session's
    disposable directory.
    """

    root = ensure_workspace_tmp(workspace)
    path = session_tmp_dir(workspace, run_id=run_id, session=session, role=role)
    values = {
        "ARCHON_HORIZON_TMP_ROOT": str(root.resolve()),
        "ARCHON_HORIZON_TMP": str(path.resolve()),
        "TMPDIR": str(path.resolve()),
        "TMP": str(path.resolve()),
        "TEMP": str(path.resolve()),
    }
    return path, values


def run_id_from_session_path(path: Path | None) -> str | None:
    """Recover a run id from a session or nested-subagent log path."""

    if path is None:
        return None
    candidate = Path(path)
    for parent in (candidate, *candidate.parents):
        if parent.parent.name == "runs":
            return parent.name
    return None


def remove_session_tmp(path: Path | None) -> None:
    """Remove one known disposable session directory, if it still exists.

    The path is accepted only when it is a directory or symlink below a
    ``tmp`` directory.  This is a defensive guard against accidentally passing
    a broad path from a caller.
    """

    if path is None:
        return
    try:
        # Do not resolve symlinks before the boundary check: resolving one could
        # turn a harmless link inside tmp into an arbitrary external target.
        candidate = Path(os.path.abspath(path))
        if candidate.name == "tmp" or candidate.parent.parent.name != "tmp":
            return
        if candidate.is_symlink():
            candidate.unlink(missing_ok=True)
        elif candidate.is_dir():
            shutil.rmtree(candidate)
    except OSError:
        # Cleanup is best effort.  The CLI's stale cleanup can reclaim a
        # partially removed directory later without masking the agent result.
        return


def _pid_alive(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_run_ids(state_path: Path) -> set[str]:
    """Return run ids whose process markers still point at live processes."""

    active: set[str] = set()
    here = socket.gethostname()
    runs = state_path / "runs"
    if not runs.is_dir():
        return active
    for marker in runs.glob("*/process.json"):
        try:
            import json

            data = json.loads(marker.read_text("utf-8"))
            if not isinstance(data, dict):
                continue
            # A marker from another host cannot be probed safely; retain it
            # conservatively rather than deleting a remote run's scratch tree.
            host = str(data.get("host") or "").strip()
            if host and host != here:
                active.add(marker.parent.name)
            elif _pid_alive(data.get("pid")):
                active.add(marker.parent.name)
        except (OSError, ValueError, TypeError):
            # A malformed marker is ambiguous: retain the corresponding run's
            # scratch until ``horizon ps --clean`` or a human resolves it.
            active.add(marker.parent.name)
            continue
    return active


def _latest_mtime(path: Path) -> float:
    """Find the newest mtime without following symlinked directories."""

    try:
        latest = path.stat().st_mtime
    except OSError:
        return 0.0
    if path.is_symlink() or not path.is_dir():
        return latest
    for child in path.iterdir():
        latest = max(latest, _latest_mtime(child))
    return latest


@dataclass(frozen=True, slots=True)
class ScratchCleanup:
    """Result of a dry-run or applied scratch cleanup."""

    candidates: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    skipped_live_runs: tuple[str, ...] = ()


def clean_workspace_tmp(
    state_path: Path,
    *,
    older_than_s: float = 24 * 60 * 60,
    apply: bool = False,
    now: float | None = None,
) -> ScratchCleanup:
    """List or remove stale scratch entries below ``state_path/tmp``.

    Run directories with a live ``process.json`` marker are always protected.
    The default is a dry run; callers must explicitly pass ``apply=True`` for
    deletion.  Entries newer than ``older_than_s`` are retained.
    """

    if older_than_s < 0:
        raise ValueError("older_than_s must be non-negative")
    root = state_path / "tmp"
    if not root.is_dir():
        return ScratchCleanup()
    cutoff = (time.time() if now is None else now) - older_than_s
    live = live_run_ids(state_path)
    candidates: list[str] = []
    removed: list[str] = []
    skipped: list[str] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if entry.is_dir() and not entry.is_symlink() and entry.name in live:
            skipped.append(entry.name)
            continue
        try:
            stale = _latest_mtime(entry) < cutoff
        except OSError:
            stale = False
        if not stale:
            continue
        rel = entry.relative_to(state_path.parent).as_posix()
        candidates.append(rel)
        if not apply:
            continue
        try:
            if entry.is_symlink() or not entry.is_dir():
                entry.unlink(missing_ok=True)
            else:
                shutil.rmtree(entry)
            removed.append(rel)
        except OSError:
            # Keep it a candidate so the caller can report that it remains.
            continue
    return ScratchCleanup(tuple(candidates), tuple(removed), tuple(sorted(skipped)))
