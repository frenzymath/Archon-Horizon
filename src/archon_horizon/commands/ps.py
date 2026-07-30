"""``horizon ps`` — which runs hold a live process, and reaping the dead ones.

Every ``horizon run`` registers itself in ``runs/<id>/process.json`` (pid,
host, started_at) and removes the marker on clean exit. So:

- marker + pid alive  → an active run (parallel with yours — be aware they
  share the workspace ledger);
- marker + pid dead   → a crashed/killed run ("zombie" marker): its state is
  fine on disk (resume it), only the marker is stale — ``--clean`` removes it;
- marker + pid alive but the run directory has seen no writes for a long time
  → likely a hung engine; ``--kill <run>`` terminates it (SIGTERM, then
  SIGKILL after a grace period).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from pathlib import Path

import typer

from archon_horizon.log import log

from .shared import emit_json

_STALE_S = 30 * 60.0  # no run-dir writes for this long ⇒ flag as inactive


def write_process_marker(run_dir: Path, **context: object) -> None:
    """Register a live run process, including optional team identity metadata."""
    try:
        (run_dir / "process.json").write_text(json.dumps({
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": time.time(),
            **{key: value for key, value in context.items() if value not in (None, "", [], ())},
        }), "utf-8")
    except OSError:
        pass


def clear_process_marker(run_dir: Path) -> None:
    try:
        (run_dir / "process.json").unlink(missing_ok=True)
    except OSError:
        pass


def _load_record(path: Path) -> dict:
    try:
        text = path.read_text("utf-8")
        if path.suffix == ".json":
            value = json.loads(text)
        else:
            from archon_horizon.store.codec import YamlCodec

            value = YamlCodec().loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _run_context(run_dir: Path, marker: dict) -> dict[str, object]:
    """Best-effort task/session label for a process marker.

    Automated runs predate the richer marker and keep their identity in run.yaml
    plus the live session's meta.json, so read both before falling back to a bare
    run id.
    """
    context: dict[str, object] = {
        key: marker.get(key)
        for key in ("task", "task_title", "session", "projects")
        if marker.get(key) not in (None, "", [], ())
    }
    meta_candidates = list((run_dir / "sessions").glob("*/meta.json"))
    if meta_candidates:
        try:
            latest_meta = max(meta_candidates, key=lambda path: path.stat().st_mtime_ns)
            meta = _load_record(latest_meta)
            context.setdefault("session", latest_meta.parent.name)
            context.setdefault("task", meta.get("task_id"))
            context.setdefault("projects", meta.get("projects") or meta.get("project"))
        except OSError:
            pass
    if not context.get("task"):
        run_path = next(iter(sorted(run_dir.glob("run.*"))), None)
        record = _load_record(run_path) if run_path else {}
        focus = record.get("focus", {})
        if isinstance(focus, dict):
            task = focus.get("task")
            tasks = focus.get("tasks")
            if not task and isinstance(tasks, list) and tasks:
                task = tasks[0]
            if task:
                context["task"] = str(task)
    task = str(context.get("task") or "").strip()
    if task and not context.get("task_title"):
        tasks_dir = run_dir.parent.parent / "tasks"
        task_path = next(iter(sorted(tasks_dir.glob(f"{task}.*"))), None)
        task_record = _load_record(task_path) if task_path else {}
        title = task_record.get("title") or task_record.get("objective")
        if title:
            context["task_title"] = str(title)
    return context


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _last_activity(run_dir: Path) -> float:
    latest = 0.0
    for dirpath, _dirnames, filenames in os.walk(run_dir):
        for name in filenames:
            try:
                latest = max(latest, os.stat(os.path.join(dirpath, name)).st_mtime)
            except OSError:
                continue
    return latest


def live_runs(runs_dir: Path, *, exclude_run: str | None = None) -> list[dict]:
    """Runs that currently hold a live (or unprobeable cross-host) process marker.

    A cheap probe for the synchronizer: reads each ``process.json`` and checks the
    pid, but skips the full run-dir walk ``_rows`` does for idle time. Local dead
    markers (zombies) are omitted; cross-host markers are included (can't probe).
    """
    out: list[dict] = []
    if not runs_dir.is_dir():
        return out
    here = socket.gethostname()
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        if exclude_run and run_dir.name == exclude_run:
            continue
        marker = run_dir / "process.json"
        if not marker.exists():
            continue
        try:
            info = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(info.get("pid") or 0)
        host = str(info.get("host") or "")
        local = host == here
        if local and pid and not _pid_alive(pid):
            continue  # zombie marker — the run is dead
        out.append({
            "run": run_dir.name,
            "pid": pid,
            "host": host,
            "local": local,
            **_run_context(run_dir, info),
        })
    return out


def _rows(runs_dir: Path) -> list[dict]:
    rows: list[dict] = []
    if not runs_dir.is_dir():
        return rows
    here = socket.gethostname()
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        marker = run_dir / "process.json"
        if not marker.exists():
            continue
        try:
            info = json.loads(marker.read_text("utf-8"))
        except (OSError, ValueError):
            info = {}
        pid = int(info.get("pid") or 0)
        host = str(info.get("host") or "")
        local = host == here
        alive = _pid_alive(pid) if (local and pid) else None  # None: can't probe cross-host
        idle_s = max(0.0, time.time() - _last_activity(run_dir))
        status = (
            "zombie-marker" if alive is False
            else "stalled" if (alive and idle_s > _STALE_S)
            else "active" if alive
            else "unknown-host"
        )
        rows.append({
            "run": run_dir.name,
            "pid": pid,
            "host": host,
            "local": local,
            "alive": alive,
            "idle_s": int(idle_s),
            "started_at": info.get("started_at"),
            "status": status,
            "marker": marker,
        })
    return rows


def ps(
    ctx: typer.Context,
    kill: str | None = typer.Option(None, "--kill", help="Terminate this run id's process (SIGTERM, then SIGKILL after 10s). Local host only."),
    clean: bool = typer.Option(False, "--clean", help="Remove stale process markers left by crashed runs (pid no longer alive)."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """List runs holding a live process; reap zombies (`--clean`) or kill a
    stuck one (`--kill <run>`). Run state on disk is never touched — a killed
    or crashed run resumes with `horizon run --resume <id>`."""
    from archon_horizon.config.loader import load_config

    root: Path = ctx.obj["root"]
    cfg = load_config(root)
    runs_dir = root / cfg.state_dir / "runs"
    rows = _rows(runs_dir)

    if kill:
        # Accept a bare number for the zero-padded on-disk id (`--kill 3` → 0003).
        wanted = f"{int(kill):04d}" if kill.isdigit() else kill.strip()
        row = next((r for r in rows if r["run"] == wanted), None)
        if row is None:
            log.error(f"No live process marker for run {kill!r}.")
            raise typer.Exit(1)
        if not row["local"]:
            log.error(f"Run {row['run']} is owned by {row['host']}; kill it there.")
            raise typer.Exit(1)
        if row["alive"]:
            os.kill(row["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and _pid_alive(row["pid"]):
                time.sleep(0.2)
            if _pid_alive(row["pid"]):
                os.kill(row["pid"], signal.SIGKILL)
            log.success(f"Terminated run {row['run']} (pid {row['pid']}). Resume with `horizon run --resume {row['run']}`.")
        row["marker"].unlink(missing_ok=True)
        return

    if clean:
        removed = []
        for row in rows:
            if row["alive"] is False:
                row["marker"].unlink(missing_ok=True)
                removed.append(row["run"])
        (log.success if removed else log.info)(
            f"Removed {len(removed)} stale marker(s): {', '.join(removed)}" if removed
            else "No stale process markers."
        )
        rows = _rows(runs_dir)

    if as_json:
        emit_json({"processes": [{k: v for k, v in r.items() if k != "marker"} for r in rows]})
        return
    if not rows:
        log.info("No runs hold a live process marker.")
        return
    log.results_table(
        [(r["run"], r["status"], f"pid {r['pid']} on {r['host']} · idle {r['idle_s']}s") for r in rows],
        title="Run processes",
    )
