"""Serialized, observable Lean checks for shared workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import typer

from archon_horizon.log import log

from .shared import emit_json


def _write_result(root: Path, result: dict[str, object], fingerprint: str) -> None:
    cache = root / ".archon-horizon" / "cache" / "checks"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / f"latest-{fingerprint}.json").write_text(
        json.dumps(result, indent=2) + "\n", "utf-8"
    )
    session_raw = os.environ.get("ARCHON_HORIZON_SESSION_DIR", "").strip()
    if not session_raw:
        return
    session = Path(session_raw).resolve()
    try:
        session.relative_to(root.resolve())
    except ValueError:
        return
    directory = session / "checks"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = str(result.get("finished_at") or "check").replace(":", "-")
    (directory / f"{stamp}-{fingerprint}.json").write_text(
        json.dumps(result, indent=2) + "\n", "utf-8"
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def check(
    ctx: typer.Context,
    targets: list[str] = typer.Argument(None, help="Lake build targets (default: the project)."),
    lean_file: Path | None = typer.Option(
        None, "--lean", help="Run `lake env lean FILE` instead of `lake build`."
    ),
    timeout: int = typer.Option(1800, "--timeout", min=1, help="Maximum check time in seconds."),
    as_json: bool = typer.Option(False, "--json", help="Emit the recorded result as JSON."),
) -> None:
    """Run one Lean check at a time and coalesce identical concurrent checks."""
    root: Path = ctx.obj["root"].resolve()
    cwd = Path.cwd().resolve()
    try:
        cwd.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Check working directory is outside the workspace: {cwd}") from exc
    targets = list(targets or [])
    if lean_file is not None and targets:
        raise ValueError("Pass either lake targets or --lean FILE, not both.")
    if lean_file is not None:
        source = lean_file.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Lean file does not exist: {source}")
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Lean file is outside the workspace: {source}") from exc
        command = ["lake", "env", "lean", str(source)]
    else:
        command = ["lake", "build", *targets]

    fingerprint = hashlib.sha256(
        json.dumps({"cwd": str(cwd), "command": command}, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    cache = root / ".archon-horizon" / "cache" / "checks"
    cache.mkdir(parents=True, exist_ok=True)
    lock_path = cache / "workspace.lock"
    requested_at = time.time()
    wait_started = time.monotonic()
    with lock_path.open("a+") as lock:
        try:
            import fcntl

            if fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB) != 0:
                raise BlockingIOError
        except BlockingIOError:
            log.info("Another Horizon Lean check is active; waiting for its resource slot.")
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        wait_seconds = time.monotonic() - wait_started

        latest_path = cache / f"latest-{fingerprint}.json"
        try:
            latest = json.loads(latest_path.read_text("utf-8"))
        except (OSError, ValueError):
            latest = {}
        if (
            wait_seconds >= 0.05
            and isinstance(latest, dict)
            and bool(latest.get("ok"))
            and float(latest.get("finished_epoch") or 0) >= requested_at
        ):
            result = {
                **latest,
                "status": "reused",
                "reused": True,
                "wait_seconds": round(wait_seconds, 3),
            }
            _write_result(root, result, fingerprint)
            if as_json:
                emit_json(result)
            else:
                log.success(f"Reused the identical completed check after waiting {wait_seconds:.1f}s.")
            return

        started = datetime.now(timezone.utc)
        start = time.monotonic()
        timed_out = False
        process = subprocess.Popen(
            command,
            cwd=cwd,
            start_new_session=True,
            stdout=sys.stderr if as_json else None,
            stderr=sys.stderr if as_json else None,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop_process(process)
            returncode = 124
        duration = time.monotonic() - start
        finished = datetime.now(timezone.utc)
        result = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "command": command,
            "cwd": cwd.relative_to(root).as_posix() or ".",
            "status": "timed_out" if timed_out else ("passed" if returncode == 0 else "failed"),
            "ok": returncode == 0,
            "returncode": returncode,
            "timed_out": timed_out,
            "reused": False,
            "wait_seconds": round(wait_seconds, 3),
            "duration_seconds": round(duration, 3),
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "finished_epoch": finished.timestamp(),
        }
        _write_result(root, result, fingerprint)
    if as_json:
        emit_json(result)
    elif returncode == 0:
        log.success(f"Lean check passed in {duration:.1f}s.")
    else:
        log.error(f"Lean check {result['status']} after {duration:.1f}s (exit {returncode}).")
    if returncode != 0:
        raise typer.Exit(returncode)
