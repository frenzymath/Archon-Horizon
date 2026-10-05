"""External liveness supervision; service restarts are explicit operator opt-in."""

from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import httpx


def check(config, *, service: str, threshold: int = 3, cooldown_seconds: int = 300,
          client=None, runner=subprocess.run, now: float | None = None):
    if not re.fullmatch(r"archon-horizon-pipeline-[a-z0-9-]+\.service", service):
        raise ValueError("watchdog may supervise only an explicitly named archon-horizon-pipeline user service")
    if not 1 <= threshold <= 20 or not 30 <= cooldown_seconds <= 86400:
        raise ValueError("invalid watchdog failure threshold or restart cooldown")
    if config.listen_host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "::"):
        raise ValueError("watchdog requires a loopback or wildcard API listener")
    host = "[::1]" if ":" in config.listen_host else "127.0.0.1"
    owned = client is None
    probe = client or httpx.Client(timeout=httpx.Timeout(5, connect=3), follow_redirects=False, trust_env=False)
    healthy = False
    try:
        response = probe.get(f"http://{host}:{config.listen_port}/health/live")
        healthy = response.status_code == 200 and response.json().get("status") == "alive"
    except (httpx.HTTPError, ValueError, AttributeError):
        pass
    finally:
        if owned:
            probe.close()
    return _record_check(config.state_root / "watchdog", service=service, healthy=healthy,
        threshold=threshold, cooldown_seconds=cooldown_seconds, runner=runner, now=now)


def check_worker(config, *, service: str, threshold: int = 3, cooldown_seconds: int = 300,
                 runner=subprocess.run, now: float | None = None, monotonic: float | None = None):
    """Supervise independent progress, including deadlocks in an otherwise live daemon."""
    if not re.fullmatch(r"(?:archon-)?horizon-pipeline-worker(?:-[a-z0-9-]+)?\.service", service):
        raise ValueError("worker watchdog requires an explicitly named Horizon worker service")
    if not 1 <= threshold <= 20 or not 30 <= cooldown_seconds <= 86400:
        raise ValueError("invalid watchdog failure threshold or restart cooldown")
    healthy = False
    reason = "Worker progress is missing or invalid"
    monotonic = time.monotonic() if monotonic is None else monotonic
    try:
        from .worker.provider import process_identity
        progress = json.loads((config.journal_root / "worker-progress.json").read_bytes())
        pid = progress["pid"]
        identity = progress["process_identity"]
        if type(pid) is not int or pid <= 0 or not identity or process_identity(pid) != identity:
            reason = "Worker process identity changed"
        elif progress["boot_id"] != Path("/proc/sys/kernel/random/boot_id").read_text().strip():
            reason = "Worker progress predates this boot"
        else:
            components = progress["components"]
            stale = []
            maximum_deadline = 120
            for name, component in components.items():
                age = monotonic - float(component["monotonic"])
                deadline = float(component["deadline_seconds"])
                maximum_deadline = max(maximum_deadline, deadline)
                if not math.isfinite(age) or not math.isfinite(deadline) or not 0 <= age <= deadline or deadline <= 0:
                    stale.append(name)
            age = monotonic - float(progress["updated_monotonic"])
            healthy = bool(components) and not stale and math.isfinite(age) and 0 <= age <= maximum_deadline
            reason = "Worker progressing" if healthy else "Stalled worker components: " + ", ".join(stale or ["progress writer"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    result = _record_check(config.journal_root / "watchdog", service=service, healthy=healthy,
        threshold=threshold, cooldown_seconds=cooldown_seconds, runner=runner, now=now)
    result["reason"] = reason
    if healthy:
        blocked = [name for name, component in components.items()
                   if component.get("phase") == "diagnostic_storage_pressure"]
        if blocked:
            result["reason"] = "Worker responsive but admission is blocked by storage pressure"
            result["admission_blocked"] = True
    return result


def _record_check(root, *, service, healthy, threshold, cooldown_seconds, runner, now):
    now = time.time() if now is None else now
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = root / f"{service}.json"
    with (root / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = {"failures": 0, "last_restart": None}
        if target.exists():
            try:
                candidate = json.loads(target.read_bytes())
                if (isinstance(candidate, dict) and type(candidate.get("failures")) is int
                        and candidate["failures"] >= 0 and (candidate.get("last_restart") is None
                        or (type(candidate["last_restart"]) in (int, float) and math.isfinite(candidate["last_restart"])))):
                    previous = candidate
            except (ValueError, OSError):
                # Corrupt counters never cause an immediate service restart.
                pass
        failures = 0 if healthy else int(previous.get("failures", 0)) + 1
        last_restart = previous.get("last_restart")
        restart = failures >= threshold and (last_restart is None or now - last_restart >= cooldown_seconds)
        state = {"schema_version": 1, "service": service, "failures": failures,
                 "last_restart": now if restart else last_restart, "observed_at": now}
        fd, temporary = tempfile.mkstemp(prefix=".watchdog-", dir=root)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(root, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            Path(temporary).unlink(missing_ok=True)
    result = {"healthy": healthy, "consecutive_failures": failures, "restart_requested": restart, "service": service}
    if restart:
        # try-restart respects an operator who deliberately stopped the service.
        response = runner(["systemctl", "--user", "--no-block", "try-restart", service], capture_output=True, timeout=30)
        result["restart_accepted"] = response.returncode == 0
    return result
