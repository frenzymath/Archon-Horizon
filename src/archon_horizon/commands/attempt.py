"""Preserve rejected or incomplete work as a session artifact."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import typer

from archon_horizon.log import log

from .shared import emit_json


app = typer.Typer(help="Preserve and inspect rejected session attempts.", no_args_is_help=True)


def _session_dir(root: Path, explicit: Path | None) -> Path:
    raw = explicit or (
        Path(os.environ["ARCHON_HORIZON_SESSION_DIR"])
        if os.environ.get("ARCHON_HORIZON_SESSION_DIR")
        else None
    )
    if raw is None:
        raise ValueError("No active Horizon session; pass --session-dir explicitly.")
    resolved = raw.expanduser().resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Session directory is outside the workspace: {resolved}") from exc
    if not resolved.is_dir():
        raise FileNotFoundError(f"Session directory does not exist: {resolved}")
    return resolved


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value[:48] or "attempt"


def _create_attempt_dir(session_dir: Path, reason: str) -> Path:
    parent = session_dir / "attempts"
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / ".lock").open("a+") as lock:
        try:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        indexes = []
        for child in parent.iterdir():
            match = re.match(r"^(\d+)-", child.name)
            if match:
                indexes.append(int(match.group(1)))
        target = parent / f"{max(indexes, default=0) + 1:04d}-{_slug(reason)}"
        target.mkdir()
        return target


@app.command("save")
def save(
    ctx: typer.Context,
    files: list[Path] = typer.Argument(
        ..., help="Draft files to preserve before replacing or deleting them."
    ),
    reason: str = typer.Option(..., "--reason", "-r", help="Why this approach was rejected or paused."),
    diagnostics: Path | None = typer.Option(
        None, "--diagnostics", help="Optional compiler/test output to preserve."
    ),
    session_dir: Path | None = typer.Option(None, "--session-dir", hidden=True),
    as_json: bool = typer.Option(False, "--json", help="Emit the manifest as JSON."),
) -> None:
    """Copy draft sources and diagnostics into the current session's attempts."""
    root: Path = ctx.obj["root"].resolve()
    reason = reason.strip()
    if not reason:
        raise ValueError("Attempt reason must not be empty.")
    resolved_files: list[tuple[Path, Path]] = []
    for source in files:
        resolved = source.expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Attempt source does not exist: {resolved}")
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Attempt source is outside the workspace: {resolved}") from exc
        resolved_files.append((resolved, relative))
    diagnostics_path = diagnostics.expanduser().resolve() if diagnostics else None
    if diagnostics_path is not None and not diagnostics_path.is_file():
        raise FileNotFoundError(f"Diagnostics file does not exist: {diagnostics_path}")

    target = _create_attempt_dir(_session_dir(root, session_dir), reason)
    manifest_files: list[dict[str, object]] = []
    for source, relative in resolved_files:
        destination = target / "files" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        manifest_files.append({
            "path": relative.as_posix(),
            "artifact": destination.relative_to(target).as_posix(),
            "bytes": source.stat().st_size,
            "lines": len(source.read_text("utf-8", errors="replace").splitlines()),
        })
    diagnostics_ref = ""
    if diagnostics_path is not None:
        destination = target / "diagnostics.txt"
        shutil.copy2(diagnostics_path, destination)
        diagnostics_ref = destination.name
    manifest = {
        "schema_version": 1,
        "status": "rejected",
        "reason": reason.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": manifest_files,
        "diagnostics": diagnostics_ref,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", "utf-8")
    payload = {**manifest, "artifact_dir": target.as_posix()}
    if as_json:
        emit_json(payload)
    else:
        log.success(f"Preserved rejected attempt in {target}")


@app.command("list")
def list_attempts(
    ctx: typer.Context,
    session_dir: Path | None = typer.Option(None, "--session-dir", hidden=True),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List preserved attempts for the current session."""
    root: Path = ctx.obj["root"].resolve()
    attempts: list[dict[str, object]] = []
    parent = _session_dir(root, session_dir) / "attempts"
    if parent.is_dir():
        for manifest_path in sorted(parent.glob("*/manifest.json")):
            try:
                data = json.loads(manifest_path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                attempts.append({**data, "artifact_dir": manifest_path.parent.as_posix()})
    if as_json:
        emit_json({"attempts": attempts, "total": len(attempts)})
        return
    if not attempts:
        log.info("No preserved attempts for this session.")
        return
    for attempt in attempts:
        log.info(f"{attempt.get('created_at', '')}  {attempt.get('reason', '')}")
