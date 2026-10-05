"""Managed Lean checks using Lake and the host's shared local cache."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import errno
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from . import build_engine
from .cache_retention import (DEFAULT_MAX_BYTES, DEFAULT_MAX_AGE_SECONDS, DEFAULT_NATIVE_MAX_BYTES,
                              DEFAULT_NATIVE_MIN_AGE_SECONDS, artifact_cache_lease, prune_native_outputs)


@dataclass(frozen=True)
class LeanBuildPolicy:
    root: Path
    max_parallel_builds: int = 1
    timeout_seconds: float = 1800
    queue_timeout_seconds: float = 30
    minimum_free_bytes: int = 1024**3
    # The build root is a host-local shared cache.  Keeping Lake artifacts
    # enabled avoids one copy of mathlib outputs per worker/worktree.
    artifact_cache: bool = True
    cache_max_bytes: int = DEFAULT_MAX_BYTES
    cache_max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    native_cache_max_bytes: int = DEFAULT_NATIVE_MAX_BYTES
    native_cache_min_age_seconds: int = DEFAULT_NATIVE_MIN_AGE_SECONDS

    def __post_init__(self) -> None:
        if not self.root.is_absolute() or self.root == Path("/") or ".." in self.root.parts:
            raise ValueError("Lean build root must be an explicit absolute directory")
        if type(self.max_parallel_builds) is not int or not 1 <= self.max_parallel_builds <= 64:
            raise ValueError("Lean build concurrency must be between 1 and 64")
        if (not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0
                or not math.isfinite(self.queue_timeout_seconds) or self.queue_timeout_seconds < 0):
            raise ValueError("Lean build deadlines must be finite and nonnegative, with a positive timeout")
        if type(self.minimum_free_bytes) is not int or self.minimum_free_bytes < 0:
            raise ValueError("Lean build free-space reserve must be nonnegative")
        if type(self.artifact_cache) is not bool:
            raise ValueError("Lean artifact cache must be a boolean")
        if any(type(value) is not int or value < 0 for value in (self.cache_max_bytes, self.cache_max_age_seconds,
                                                               self.native_cache_max_bytes, self.native_cache_min_age_seconds)):
            raise ValueError("Lean cache retention bounds must be nonnegative integers")

    def environment(self, harness_id: str) -> dict[str, str]:
        if not harness_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in harness_id):
            raise ValueError("invalid Lean cache namespace")
        # The default lane is host-wide: every worker using the same build root
        # reuses the same Lake/mathlib artifacts. Operators can set an explicit
        # namespace when a toolchain must be isolated deliberately.
        namespace = os.environ.get("HORIZON_BUILD_CACHE_NAMESPACE", "host")
        if not namespace or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in namespace):
            raise ValueError("invalid Lean cache namespace")
        return {"HORIZON_LEAN_BUILD": json.dumps({**asdict(self), "root": str(self.root)}),
                "HORIZON_BUILD_CACHE_NAMESPACE": namespace}


def check(root: Path, targets: list[str], policy: LeanBuildPolicy, *,
          lean_file: str | None = None, probe: bool = False) -> dict:
    started = time.monotonic()
    timings = {}
    previous_signal = None
    if threading.current_thread() is threading.main_thread():
        def interrupted(*_):
            raise KeyboardInterrupt
        previous_signal = signal.signal(signal.SIGTERM, interrupted)
    try:
        result = _check(root, targets, policy, lean_file=lean_file, probe=probe, timings=timings)
    except build_engine.CheckDeferred as error:
        result = {"ok": False, "status": "deferred", "returncode": 75, "error": str(error)}
    except (TimeoutError, subprocess.TimeoutExpired):
        result = {"ok": False, "status": "timed_out", "returncode": 124, "error": "Lean check deadline reached"}
    except KeyboardInterrupt:
        result = {"ok": False, "status": "cancelled", "returncode": 130, "error": "Lean check interrupted"}
    except OSError as error:
        if error.errno not in {errno.ENOSPC, errno.EDQUOT}:
            raise
        result = {"ok": False, "status": "deferred", "returncode": 75,
                "error": "Lean build storage is exhausted; retry after capacity is restored"}
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)
    result["timings"] = {**result.get("timings", {}), **timings,
                         "total_seconds": time.monotonic() - started}
    return result


@contextmanager
def _measure(timings, key):
    started = time.monotonic()
    try:
        yield
    finally:
        timings[key] = timings.get(key, 0) + time.monotonic() - started


def _check(root: Path, targets: list[str], policy: LeanBuildPolicy, *, lean_file=None, probe=False, timings=None) -> dict:
    timings = timings if timings is not None else {}
    preparation_started = time.monotonic()
    deadline = time.monotonic() + policy.timeout_seconds
    root = root.resolve(strict=True)
    if lean_file and (Path(lean_file).is_absolute() or not (root / lean_file).resolve().is_relative_to(root)):
        raise ValueError("Lean file must be inside the selected project")
    policy.root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ,
           "HORIZON_LEAN_CACHE_ROOT": str(policy.root),
           "HORIZON_BUILD_MINIMUM_FREE_BYTES": str(policy.minimum_free_bytes),
           "HORIZON_BUILD_SLOTS": str(policy.max_parallel_builds),
           "HORIZON_BUILD_CACHE_MAX_BYTES": str(policy.cache_max_bytes),
           "HORIZON_BUILD_CACHE_MAX_AGE_SECONDS": str(policy.cache_max_age_seconds),
           "HORIZON_BUILD_NATIVE_MAX_BYTES": str(policy.native_cache_max_bytes),
           "HORIZON_BUILD_NATIVE_MIN_AGE_SECONDS": str(policy.native_cache_min_age_seconds),
           "LAKE_ARTIFACT_CACHE": "true" if policy.artifact_cache else "false",
           "LAKE_RESTORE_ARTIFACTS": "true"}
    env.update(build_engine.build_environment(policy.root, env))
    try:
        with artifact_cache_lease(policy.root, env):
            try:
                build_engine.check_storage(root, env)
            except build_engine.CheckDeferred:
                with build_engine.resource_lock(build_engine.checkout_paths(root), deadline,
                                                 build_engine.CheckProgress(policy.queue_timeout_seconds)):
                    prune_native_outputs(root, env)
                build_engine.check_storage(root, env)
    except build_engine.CheckDeferred as error:
        timings["preparation_seconds"] = time.monotonic() - preparation_started
        return {"ok": False, "status": "deferred", "returncode": 75, "error": str(error)}
    version = build_engine.captured_command(["lake", "--version"], root, env, min(deadline, time.monotonic() + 20))
    timings["preparation_seconds"] = time.monotonic() - preparation_started
    if version.returncode:
        return {"ok": False, "status": "failed", "returncode": version.returncode,
                "error": "Lake toolchain is unavailable; check the pinned toolchain installation"}
    version_text = version.stdout.decode(errors="replace")
    with _measure(timings, "source_fingerprint_seconds"):
        before = build_engine.source_identity(root, env, version_text, deadline=deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Lean check timed out preparing source")
    with _measure(timings, "engine_seconds"):
        result = build_engine.run_check(root, targets, lean_file=lean_file, env=env,
                                       timeout=remaining,
                                       queue_timeout=policy.queue_timeout_seconds, probe=probe,
                                       minimum_free_bytes=policy.minimum_free_bytes)
    after = None
    if result["returncode"] not in {75, 124, 130}:
        with _measure(timings, "source_fingerprint_seconds"):
            after = build_engine.source_identity(root, env, version_text, deadline=deadline)
    result.update(toolchain=version_text.strip(), source_key=after,
                  snapshot_verified=bool(before and before == after))
    if result["returncode"] == 130:
        result["status"] = "cancelled"
    if result["ok"] and before and before != after:
        result.update(ok=False, status="deferred", returncode=75,
                      error="Source changed during the check; check the updated snapshot")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*", help="Lake targets; omit for the project's default targets")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--lean", help="Check one file; imported modules must already be built")
    parser.add_argument("--probe", action="store_true", help="Check readiness without compiling")
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        raw = json.loads(os.environ.get("HORIZON_LEAN_BUILD", "null"))
        if not isinstance(raw, dict):
            raise ValueError("This host has no managed Lean build policy")
        policy = LeanBuildPolicy(**{**raw, "root": Path(raw["root"])})
        result = check(args.root, args.targets, policy, lean_file=args.lean, probe=args.probe)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        result = {"ok": False, "status": "failed", "returncode": 1,
                  "error": f"Lean check setup failed ({type(error).__name__})"}
        if isinstance(error, ValueError):
            result["error"] = str(error)
        result["timings"] = {"total_seconds": time.monotonic() - started}
    print(json.dumps(result, sort_keys=True))
    return result["returncode"]


if __name__ == "__main__":
    sys.exit(main())
