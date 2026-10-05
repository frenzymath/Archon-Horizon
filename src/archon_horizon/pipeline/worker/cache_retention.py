"""Bound the rebuildable Lake artifact store without touching Git or worktrees."""

from contextlib import contextmanager
import fcntl
import heapq
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import time


DEFAULT_MAX_BYTES = 10 * 1024**3
DEFAULT_MAX_AGE_SECONDS = 7 * 86400
DEFAULT_NATIVE_MAX_BYTES = 4 * 1024**3
DEFAULT_NATIVE_MIN_AGE_SECONDS = 7 * 86400


def _recent(marker: Path, policy: str, root: Path, reserve: int) -> bool:
    try:
        usage = os.statvfs(root)
        return (time.time() - marker.stat().st_mtime < 300 and marker.read_text() == policy
                and usage.f_bavail * usage.f_frsize >= reserve)
    except OSError:
        return False


def prune_native_outputs(checkout: Path, env: dict) -> None:
    """Called only with the checkout build lock held; retain recent native output.

    Compiled Lean interfaces, Git-tracked files and every source directory are
    outside this scope. Native C/object output and Lean's generated IR metadata
    can be rebuilt from those inputs.
    """
    root = checkout / ".lake" / "build" / "ir"
    if any(path.is_symlink() for path in (checkout / ".lake", root.parent, root)) or not root.is_dir():
        return
    maximum = int(env.get("HORIZON_BUILD_NATIVE_MAX_BYTES", DEFAULT_NATIVE_MAX_BYTES))
    minimum_age = int(env.get("HORIZON_BUILD_NATIVE_MIN_AGE_SECONDS", DEFAULT_NATIVE_MIN_AGE_SECONDS))
    reserve = int(env.get("HORIZON_BUILD_MINIMUM_FREE_BYTES", "0"))
    if min(maximum, minimum_age, reserve) < 0:
        raise ValueError("Lean native cache retention bounds must be nonnegative")
    marker = checkout / ".lake" / "horizon-native-retention.checked"
    # Bump the marker format when the disposable file set changes so an old
    # native-only marker cannot suppress the first IR cleanup pass.
    policy = f"ir-v2:{maximum}:{minimum_age}:{reserve}"
    if marker.is_symlink() or _recent(marker, policy, root, reserve):
        return
    try:
        if _prune_native(checkout, root, maximum, minimum_age, reserve):
            marker.write_text(policy)
    except (OSError, subprocess.TimeoutExpired):
        logging.warning("Native output retention deferred; build and recovery data preserved")


def _prune_native(checkout: Path, root: Path, maximum: int, minimum_age: int, reserve: int) -> bool:
    result = subprocess.run(["git", "-C", str(checkout), "ls-files", "-z", "--", ".lake/build/ir"],
                            capture_output=True, timeout=10)
    if result.returncode:
        return False
    tracked = {os.fsdecode(path) for path in result.stdout.split(b"\0") if path}
    cutoff = time.time() - minimum_age
    deadline = time.monotonic() + 10
    while True:
        total, oldest = 0, []
        for directory, subdirs, files in os.walk(root, followlinks=False):
            subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
            for name in files:
                if time.monotonic() >= deadline:
                    return False
                path = Path(directory) / name
                # Lean writes a large JSON/hash pair for every generated IR
                # module.  These files are disposable too, but only inside the
                # dedicated IR directory; other JSON files may be source data.
                ir_metadata = root in path.parents and name.endswith((".json", ".hash"))
                native_output = name.endswith((".c", ".o", ".o.export", ".bc", ".ll"))
                if not native_output and not ir_metadata:
                    continue
                if path.relative_to(checkout).as_posix() in tracked:
                    continue
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                size = info.st_blocks * 512
                total += size
                if info.st_mtime > cutoff:
                    continue
                entry = (-info.st_mtime_ns, str(path), info.st_ino, size)
                if len(oldest) < 2048:
                    heapq.heappush(oldest, entry)
                elif entry > oldest[0]:
                    heapq.heapreplace(oldest, entry)
        if not oldest:
            return True
        deleted = False
        for negative_mtime, filename, inode, size in sorted(oldest, reverse=True):
            if time.monotonic() >= deadline:
                return False
            usage = os.statvfs(root)
            if total <= maximum and usage.f_bavail * usage.f_frsize >= reserve:
                return True
            path = Path(filename)
            try:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_ino != inode or info.st_mtime_ns != -negative_mtime:
                    continue
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
            deleted = True
        if not deleted:
            return True


def _files(root: Path):
    # Only these dedicated stores are disposable. Git pools contain objects
    # borrowed by --shared clones and cannot be treated as caches for eviction.
    for lane in root.iterdir():
        if not re.fullmatch(r"[A-Za-z0-9_-]+", lane.name) or lane.is_symlink() or not lane.is_dir():
            continue
        lake = lane / "lake"
        if lake.is_symlink() or not lake.is_dir():
            continue
        for directory, subdirs, files in os.walk(lake, followlinks=False):
            subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
            for name in files:
                path = Path(directory) / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    yield path, info


def _prune(root: Path, *, max_bytes: int, max_age_seconds: int, minimum_free_bytes: int) -> bool:
    cutoff = time.time() - max_age_seconds
    deadline = time.monotonic() + 10
    while True:
        total = 0
        oldest = []
        # Keep memory bounded even when a cache contains millions of objects.
        # Additional passes are only needed after large one-off accumulation.
        for path, info in _files(root):
            if time.monotonic() >= deadline:
                return False
            size = info.st_blocks * 512
            total += size
            entry = (-info.st_mtime_ns, str(path), info.st_ino, size)
            if len(oldest) < 2048:
                heapq.heappush(oldest, entry)
            elif entry > oldest[0]:
                heapq.heapreplace(oldest, entry)
        if not oldest:
            return True
        deleted = False
        for negative_mtime, filename, inode, size in sorted(oldest, reverse=True):
            if time.monotonic() >= deadline:
                return False
            usage = os.statvfs(root)
            free = usage.f_bavail * usage.f_frsize
            if total <= max_bytes and -negative_mtime / 1e9 >= cutoff and free >= minimum_free_bytes:
                return True
            path = Path(filename)
            try:
                current = path.lstat()
                if (not stat.S_ISREG(current.st_mode) or current.st_ino != inode
                        or current.st_mtime_ns != -negative_mtime):
                    continue
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
            deleted = True
        if not deleted:
            return True


def _maintain(lock, root: Path, env: dict) -> None:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    try:
        max_bytes = int(env.get("HORIZON_BUILD_CACHE_MAX_BYTES", DEFAULT_MAX_BYTES))
        max_age = int(env.get("HORIZON_BUILD_CACHE_MAX_AGE_SECONDS", DEFAULT_MAX_AGE_SECONDS))
        reserve = int(env.get("HORIZON_BUILD_MINIMUM_FREE_BYTES", "0"))
        if min(max_bytes, max_age, reserve) < 0:
            raise ValueError("Lean cache retention bounds must be nonnegative")
        marker = root / ".artifact-retention-checked"
        policy = f"{max_bytes}:{max_age}:{reserve}"
        if marker.is_symlink() or _recent(marker, policy, root, reserve):
            return
        if _prune(root, max_bytes=max_bytes, max_age_seconds=max_age, minimum_free_bytes=reserve):
            marker.write_text(policy)
    except OSError:
        logging.warning("Lake artifact retention deferred; build and recovery data preserved")
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)


@contextmanager
def artifact_cache_lease(root: Path, env: dict):
    """All managed cache users share a lease; eviction requires exclusive access."""
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".artifact-retention.lock").open("a+") as lock:
        _maintain(lock, root, env)
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for Lean cache maintenance")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            _maintain(lock, root, env)
