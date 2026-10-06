"""Local Lean checks, shared dependency objects, and host build reservations."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Mapping

from .cache_retention import artifact_cache_lease, prune_native_outputs


class CheckDeferred(RuntimeError):
    """No compiler failure: the requested check can be scheduled later."""


def check_storage(root: Path, env: Mapping[str, str]) -> None:
    reserve = int(env.get("HORIZON_BUILD_MINIMUM_FREE_BYTES", "0"))
    if reserve < 0:
        raise ValueError("Lean build free-space reserve must be nonnegative")
    if not reserve:
        return
    paths = {"source": root, "build": root / ".lake"}
    for name, key in (("cache", "HORIZON_LEAN_CACHE_ROOT"), ("artifacts", "LAKE_CACHE_DIR"), ("scratch", "TMPDIR")):
        if env.get(key):
            paths[name] = Path(env[key])
    for name, path in paths.items():
        path = path.resolve()
        while not path.exists() and path != path.parent:
            path = path.parent
        free = shutil.disk_usage(path).free
        if free < reserve:
            raise CheckDeferred(f"Lean build storage reserve reached on {name} filesystem "
                                f"({free} free bytes; {reserve} required); retry after capacity is restored")


class CheckProgress:
    def __init__(self, queue_timeout: float):
        self.queue_timeout = queue_timeout
        self.seconds = {name: 0.0 for name in ("wait", "prepare", "build")}
        self.failure: dict | None = None

    @contextmanager
    def phase(self, name: str):
        started = time.monotonic()
        waiting = self.seconds["wait"]
        try:
            yield
        finally:
            self.seconds[name] += time.monotonic() - started - (self.seconds["wait"] - waiting if name != "wait" else 0)

    def wait_deadline(self, deadline: float) -> float:
        return min(deadline, time.monotonic() + max(0, self.queue_timeout - self.seconds["wait"]))


@contextmanager
def resource_lock(paths: list[Path], deadline: float, progress: CheckProgress, *, any_slot: bool = False):
    """Reserve one host slot or all checkout paths, counting only acquisition as waiting."""
    started = time.monotonic()
    wait_until = progress.wait_deadline(deadline)
    handles = []
    try:
        for path in sorted(set(paths)):
            path.parent.mkdir(parents=True, exist_ok=True)
            handles.append(path.open("a+"))
        while True:
            acquired = []
            for handle in handles:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired.append(handle)
                    if any_slot:
                        break
                except BlockingIOError:
                    if not any_slot:
                        break
            if len(acquired) == len(handles) or any_slot and acquired:
                break
            for handle in acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                raise TimeoutError("Lean check timed out while waiting for a resource")
            if time.monotonic() >= wait_until:
                raise CheckDeferred("build resource queue deadline reached; publish pending verification and retry later")
            time.sleep(min(0.1, max(0, wait_until - time.monotonic())))
        progress.seconds["wait"] += time.monotonic() - started
        started = None
        yield
    finally:
        if started is not None:
            progress.seconds["wait"] += time.monotonic() - started
        for handle in handles:
            handle.close()


def checkout_paths(root: Path) -> list[Path]:
    roots, pending = set(), [root.resolve()]
    while pending:
        directory = pending.pop()
        if directory in roots:
            continue
        roots.add(directory)
        manifest = directory / "lake-manifest.json"
        if manifest.is_file():
            for package in json.loads(manifest.read_text()).get("packages", []):
                if package.get("type") == "path":
                    pending.append((directory / package["dir"]).resolve())
    return [directory / ".lake" / "horizon-check.lock" for directory in sorted(roots)]


def source_failure(output: str, root: Path, env: Mapping[str, str]) -> dict | None:
    # Distinguish source errors from failures that recover without source edits.
    if re.search(r"disk quota exceeded|no space left on device|EDQUOT|ENOSPC", output, re.I):
        return None
    if re.search(r"no such file or directory.*\n\s*file: [^\n]+\.lean(?:\n|$)", output):
        kind = "missing_source"
    elif re.search(r"(?:^|\n)(?:[^\s\n:]+\.lean:\d+:\d+:\s*error:|error:\s*[^\s\n:]+\.lean:\d+:\d+:)", output) and not re.search(
            r"object file|unknown module prefix|out of memory|timed out|maximum.*(?:heartbeats|recursion)|interrupted", output, re.I):
        kind = "source_error"
    else:
        return None
    diagnostic = output.replace(str(root), "<checkout>")
    for key in ("HORIZON_EXECUTION_TOKEN", "HORIZON_API_TOKEN"):
        if env.get(key):
            diagnostic = diagnostic.replace(env[key], "[redacted]")
    diagnostic = re.sub(r"(https?://)[^\s/]+:[^\s/@]+@", r"\1[redacted]@", diagnostic)
    return {"kind": kind, "returncode": 1, "diagnostic": diagnostic.encode()[:16000].decode("utf-8", errors="ignore")}


def diagnostic_summary(output: str, root: Path, env: Mapping[str, str]) -> str:
    """Return bounded, redacted compiler output for failed check telemetry."""
    diagnostic = output.replace(str(root), "<checkout>")
    for key in ("HORIZON_EXECUTION_TOKEN", "HORIZON_API_TOKEN"):
        if env.get(key):
            diagnostic = diagnostic.replace(env[key], "[redacted]")
    diagnostic = re.sub(r"(https?://)[^\s/]+:[^\s/@]+@", r"\1[redacted]@", diagnostic)
    return diagnostic.encode()[-16000:].decode("utf-8", errors="ignore")


def deferred_marker(cache_root: Path, root: Path, targets: list[str], lean_file: str | None,
                    execution_id: str) -> Path | None:
    """Identify a deferred check within one worker execution.

    A later execution gets a fresh queue budget, while repeated helper invocations can
    probe immediately without repeatedly occupying a build queue.
    """
    if not execution_id:
        return None
    identity = json.dumps([str(root), sorted(set(targets)), lean_file], separators=(",", ":"))
    digest = hashlib.sha256((execution_id + ":" + identity).encode()).hexdigest()
    return cache_root / "deferred" / (digest + ".marker")


def _digest(path: Path, deadline: float | None = None) -> str:
    with path.open("rb") as source:
        if deadline is None:
            return hashlib.file_digest(source, "sha256").hexdigest()
        digest = hashlib.sha256()
        while chunk := source.read(1024 * 1024):
            if time.monotonic() >= deadline:
                raise TimeoutError("Lean check timed out while fingerprinting source")
            digest.update(chunk)
        return digest.hexdigest()


def build_environment(host_root: str | Path, env: Mapping[str, str]) -> dict[str, str]:
    """Keep the writable Lake store out of individual worktrees and trust lanes."""
    root = Path(env.get("HORIZON_LEAN_CACHE_ROOT") or Path(host_root).expanduser().resolve() / "lean-cache")
    lane = "worker"
    if namespace := env.get("HORIZON_BUILD_CACHE_NAMESPACE"):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", namespace):
            raise ValueError("invalid Lean cache namespace")
        lane = namespace
    slots = env.get("HORIZON_BUILD_SLOTS")
    if not slots:
        try:
            cores = len(os.sched_getaffinity(0))
        except (AttributeError, OSError):
            cores = os.cpu_count() or 2
        # A Lean build can use several compiler processes. Reserve roughly a
        # quarter of the host's cores per build, with a bounded override for
        # unusually large or small machines.
        slots = str(max(1, min(8, cores // 4)))
    return {"HORIZON_LEAN_CACHE_ROOT": str(root), "LAKE_CACHE_DIR": str(root / lane / "lake"),
            "LAKE_ARTIFACT_CACHE": env.get("LAKE_ARTIFACT_CACHE", "true"),
            "LAKE_RESTORE_ARTIFACTS": env.get("LAKE_RESTORE_ARTIFACTS", "true"),
            "HORIZON_BUILD_SLOTS": slots}


def prepare_dependencies(root: Path, env: Mapping[str, str], deadline: float, progress: CheckProgress | None = None) -> None:
    """Share Git objects for pinned dependencies without sharing writable trees.

    On reflink-capable filesystems, identical source files also share disk blocks.
    Existing dependency checkouts are always preserved, including local edits.
    The caller holds the checkout lock; each Git pool has its own short reservation.
    """
    manifest_path = root / "lake-manifest.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text())
    packages = root / manifest.get("packagesDir", ".lake/packages")
    if not packages.resolve().is_relative_to(root) or packages.is_symlink():
        return
    pool = Path(env["LAKE_CACHE_DIR"]).parent / "git"
    for package in manifest.get("packages", []):
        name, revision, url = package.get("name", ""), package.get("rev", ""), package.get("url", "")
        if (package.get("type") != "git" or not re.fullmatch(r"[A-Za-z0-9_+.-]+", name)
                or name in {".", ".."} or not re.fullmatch(r"[0-9a-f]{40,64}", revision) or not url):
            continue
        destination = packages / name
        if destination.exists() or destination.is_symlink():
            continue
        pool.mkdir(parents=True, exist_ok=True)
        packages.mkdir(parents=True, exist_ok=True)
        repository = pool / (hashlib.sha256(url.encode()).hexdigest() + ".git")
        git_env = {**env, "GIT_TERMINAL_PROMPT": "0"}
        def git(args: list[str], cwd: Path, *, check: bool = True) -> subprocess.CompletedProcess:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out preparing dependencies")
            result = captured_command(["git", "-c", "gc.auto=0", *args], cwd, git_env,
                                      min(deadline, time.monotonic() + 180))
            if check and result.returncode:
                raise RuntimeError(f"could not prepare pinned dependency {name}; check its URL and revision")
            return result
        with resource_lock([repository.with_suffix(".lock")], deadline, progress or CheckProgress(60)):
            if not repository.exists():
                git(["init", "--bare", str(repository)], root)
            if git(["cat-file", "-e", revision + "^{commit}"], repository, check=False).returncode:
                fetch_url = str((root / url).resolve()) if ":" not in url and not url.startswith("/") else url
                git(["fetch", "--no-tags", fetch_url, revision + ":refs/horizon/" + revision], repository)
            with tempfile.TemporaryDirectory(dir=packages, prefix=".horizon-dependency-") as temporary:
                stage = Path(temporary) / "checkout"
                git(["clone", "--shared", "--no-checkout", str(repository), str(stage)], root)
                git(["checkout", "--detach", revision], stage)
                git(["remote", "set-url", "origin", url], stage)
                donor_record = pool / (repository.stem + "-" + revision + ".json")
                try:
                    donor = Path(json.loads(donor_record.read_text())["path"])
                    _reflink_sources(donor, stage, git(["ls-files", "-z"], stage).stdout)
                except (OSError, ValueError, KeyError):
                    pass
                try:
                    os.rename(stage, destination)
                except OSError as exc:
                    # Lake or another helper can materialize the same package
                    # after the initial existence check. Accept only the exact
                    # pinned checkout; preserve and report every other target.
                    if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise
                    installed = git(["rev-parse", "HEAD"], destination, check=False)
                    if installed.returncode or installed.stdout.decode().strip() != revision:
                        raise RuntimeError(
                            f"concurrent dependency checkout for {name} is not at its pinned revision") from exc
                donor_record.write_text(json.dumps({"path": str(destination)}))


def _reflink_sources(donor: Path, destination: Path, paths: bytes) -> None:
    for encoded in paths.split(b"\0"):
        if not encoded:
            continue
        relative = Path(os.fsdecode(encoded))
        source, target = donor / relative, destination / relative
        if not source.is_file() or source.is_symlink() or not target.is_file() or target.is_symlink():
            continue
        temporary = None
        try:
            with source.open("rb") as original, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                fcntl.ioctl(output.fileno(), 0x40049409, original.fileno())  # Linux FICLONE
            # A live donor may have edits. Only share bytes matching the Git checkout.
            if _digest(temporary) == _digest(target):
                temporary.chmod(target.stat().st_mode & 0o777)
                os.replace(temporary, target)
        except OSError:
            return  # Unsupported filesystem: retain the independent source files.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def source_identity(root: Path, env: Mapping[str, str], version: str, *, deadline: float | None = None) -> str | None:
    """Hash actual source bytes, including uncommitted edits, before and after a build.

    Include existing path dependencies and reject ambiguous Git snapshots.
    Lake checks restored module inputs independently.
    """
    if not (root / "lake-manifest.json").is_file():
        return None
    manifest = json.loads((root / "lake-manifest.json").read_text())
    packages = manifest.get("packages", [])
    if any(p.get("type") not in {"git", "path"} or
           (p.get("type") == "git" and not re.fullmatch(r"[0-9a-f]{40,64}", p.get("rev", "")))
           for p in packages):
        return None
    files = {}
    deadline = deadline if deadline is not None else time.monotonic() + 180
    context = {**os.environ, **env}
    def probe(argv, cwd=root, seconds=30):
        return captured_command(argv, cwd, context, min(deadline, time.monotonic() + seconds))
    listing = probe(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    if listing.returncode:
        return None
    for encoded in sorted(set(listing.stdout.split(b"\0")) - {b""}):
        name = os.fsdecode(encoded)
        if any(part in {".git", ".lake"} for part in Path(name).parts):
            continue
        path = root / name
        if path.is_symlink() or path.is_dir():
            return None
        files[name] = _digest(path, deadline) if path.is_file() else None
    for name in ("lean-toolchain", "lake-manifest.json", "lakefile.lean", "lakefile.toml"):
        if (root / name).is_file():
            files[name] = _digest(root / name, deadline)
    if not files:
        return None
    for package in packages:
        if package.get("type") == "git":
            directory = root / manifest.get("packagesDir", ".lake/packages") / package["name"]
            if directory.exists():
                status = probe(["git", "status", "--porcelain", "--untracked-files=normal"], directory)
                revision = probe(["git", "rev-parse", "HEAD"], directory)
                if status.returncode or status.stdout or revision.stdout.decode().strip() != package["rev"]:
                    return None
        else:
            directory = (root / str(package.get("dir") or "")).resolve()
            if not directory.is_dir():
                return None
            for path in sorted(directory.rglob("*")):
                if time.monotonic() >= deadline:
                    raise TimeoutError("Lean check timed out while fingerprinting dependencies")
                relative = path.relative_to(directory)
                if any(part in {".git", ".lake"} for part in relative.parts):
                    continue
                if path.is_symlink():
                    return None
                if path.is_file():
                    files[f"@path/{package['name']}/{relative.as_posix()}"] = _digest(path, deadline)
    ignored_env = {"LAKE_CACHE_DIR", "LAKE_CACHE_KEY", "LAKE_CACHE_SERVICE", "LAKE_CACHE_ARTIFACT_ENDPOINT",
                   "LAKE_CACHE_REVISION_ENDPOINT", "LEAN_SRC_PATH"}
    settings = {key: value for key, value in env.items()
                if (key.startswith(("LEAN_", "LAKE_")) and key not in ignored_env)
                or key in {"CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS", "CPATH", "C_INCLUDE_PATH",
                           "CPLUS_INCLUDE_PATH", "LIBRARY_PATH", "LD_LIBRARY_PATH", "SOURCE_DATE_EPOCH",
                           "ELAN_TOOLCHAIN", "HORIZON_BUILD_ENV_KEY"}}
    compilers = {}
    for label, command in (("c", env.get("LEAN_CC") or env.get("CC") or "cc"), ("c++", env.get("CXX") or "c++")):
        try:
            result = probe([*shlex.split(command), "--version"], seconds=10)
            compilers[label] = {"status": result.returncode, "version": result.stdout[:4096].decode(errors="replace")}
        except (OSError, subprocess.TimeoutExpired):
            compilers[label] = {"status": "unavailable"}
    payload = {"version": 1, "lake": version, "platform": [platform.system(), platform.machine(), platform.libc_ver()],
               "files": files, "settings": settings, "compilers": compilers}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stop(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    except ProcessLookupError:
        pass
    # The leader may exit promptly while a compiler grandchild ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _command(argv: list[str], root: Path, env: Mapping[str, str], deadline: float,
             *, quiet: bool = False,
             diagnostics: list[str] | None = None, stdout_sink: bytearray | None = None) -> int:
    if time.monotonic() >= deadline:
        raise TimeoutError("Lean check timed out")
    capture = diagnostics is not None
    output = subprocess.PIPE if capture else subprocess.DEVNULL if quiet else sys.stderr
    process = None
    first, tail, total, forwarded = b"", b"", 0, 0
    output_limit = 256 * 1024
    selector = selectors.DefaultSelector() if capture else None
    def consume(chunk, stream="stderr"):
        nonlocal first, tail, total, forwarded
        if stdout_sink is not None and stream == "stdout":
            if len(stdout_sink) + len(chunk) > 16 * 1024**2:
                raise CheckDeferred("Lean preparation output exceeded its 16 MiB bound")
            stdout_sink.extend(chunk)
            return
        first = (first + chunk)[:8192]
        tail = (tail + chunk)[-8192:]
        total += len(chunk)
        if not quiet and forwarded < output_limit:
            part = chunk[:output_limit - forwarded]
            sys.stderr.write(part.decode("utf-8", errors="replace"))
            forwarded += len(part)
            if forwarded == output_limit:
                sys.stderr.write("\n[Further build output omitted; bounded diagnostic tail retained.]\n")
    try:
        check_storage(root, env)
        process = subprocess.Popen(argv, cwd=root, env=env, stdout=output,
                                   stderr=subprocess.PIPE if stdout_sink is not None else output, process_group=0)
        if capture:
            # Both streams share one pipe. Drain it continuously without a growing
            # tempfile, so compiler diagnostics cannot themselves fill the disk.
            selector.register(process.stdout, selectors.EVENT_READ, "stdout" if stdout_sink is not None else "stderr")
            os.set_blocking(process.stdout.fileno(), False)
            if stdout_sink is not None:
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                os.set_blocking(process.stderr.fileno(), False)
        while True:
            check_storage(root, env)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Lean check timed out")
            if capture:
                for key, _ in selector.select(timeout=min(0.2, remaining)):
                    chunk = os.read(key.fd, 65536)
                    if chunk:
                        consume(chunk, key.data)
                    else:
                        selector.unregister(key.fileobj)
                code = process.poll()
                if code is not None:
                    _stop(process)
                    for stream, pipe in (("stdout" if stdout_sink is not None else "stderr", process.stdout),
                                         ("stderr", process.stderr)):
                        if pipe is not None:
                            while chunk := pipe.read(65536):
                                consume(chunk, stream)
                    return code
                continue
            try:
                code = process.wait(timeout=min(0.2, remaining))
                _stop(process)
                return code
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        if process is not None:
            _stop(process)
        raise
    finally:
        if selector is not None:
            selector.close()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if process is not None and process.stderr is not None:
            process.stderr.close()
        if capture:
            diagnostics.append((first + (b"\n...\n" + tail if total > 8192 else b"")).decode("utf-8", errors="replace"))


def captured_command(argv: list[str], root: Path, env: Mapping[str, str], deadline: float) -> subprocess.CompletedProcess:
    output, diagnostics = bytearray(), []
    code = _command(argv, root, env, deadline, quiet=True, diagnostics=diagnostics, stdout_sink=output)
    return subprocess.CompletedProcess(argv, code, bytes(output), "\n".join(diagnostics).encode())


def _build_command(argv: list[str], root: Path, env: Mapping[str, str], deadline: float,
                   progress: CheckProgress,
                   diagnostics: list[str] | None = None) -> int:
    slots = int(env.get("HORIZON_BUILD_SLOTS", "2"))
    if not 1 <= slots <= 64:
        raise ValueError("HORIZON_BUILD_SLOTS must be between 1 and 64")
    paths = [Path(env["HORIZON_LEAN_CACHE_ROOT"]) / "slots" / f"{i}.lock" for i in range(slots)]
    with resource_lock(paths, deadline, progress, any_slot=True), progress.phase("build"):
        return _command(argv, root, env, deadline, **({"diagnostics": diagnostics} if diagnostics is not None else {}))


def run_check(root: str | Path, targets: list[str] | None = None, *, lean_file: str | None = None,
              timeout: float = 1800, env: Mapping[str, str] | None = None,
              queue_timeout: float = 60, probe: bool = False, minimum_free_bytes: int | None = None) -> dict:
    root = Path(root).resolve()
    targets = targets or []
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if not math.isfinite(queue_timeout) or queue_timeout < 0:
        raise ValueError("queue_timeout must be a finite nonnegative number")
    if probe and lean_file:
        raise ValueError("--probe checks Lake targets, not --lean files")
    if any(target.startswith("-") for target in targets) or lean_file and targets:
        raise ValueError("specify Lake targets or --lean; compiler flags are not targets")
    context = {**os.environ, **dict(env or {})}
    if minimum_free_bytes is not None:
        if type(minimum_free_bytes) is not int or minimum_free_bytes < 0:
            raise ValueError("Lean build free-space reserve must be a nonnegative integer")
        context["HORIZON_BUILD_MINIMUM_FREE_BYTES"] = str(minimum_free_bytes)
    host_root = context.get("HORIZON_LEAN_CACHE_ROOT") or str(
        Path(context.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "horizon")
    context.update(build_environment(host_root, context))
    cache_root = Path(context["HORIZON_LEAN_CACHE_ROOT"])
    started = time.monotonic()
    deadline = started + timeout
    progress = CheckProgress(queue_timeout)
    marker = deferred_marker(cache_root, root, targets, lean_file, context.get("HORIZON_EXECUTION_ID", ""))
    repeated_deferred = bool(marker and marker.is_file())
    if repeated_deferred:
        progress.queue_timeout = 0
    command = ["lake", "env", "lean", str((root / lean_file).resolve())] if lean_file else ["lake", "build", *targets]
    check_details = {"mode": "lean" if lean_file else "lake", "command": command, "cwd": str(root),
                     "targets": targets, "lean_file": lean_file}
    code, error = 1, None
    diagnostics: list[str] = []
    previous_signal = None
    if threading.current_thread() is threading.main_thread():
        def interrupted(*_):
            raise KeyboardInterrupt
        previous_signal = signal.signal(signal.SIGTERM, interrupted)
    try:
        with artifact_cache_lease(cache_root, context):
            with resource_lock(checkout_paths(root), deadline, progress):
                prune_native_outputs(root, context)
            check_storage(root, context)
            if probe:
                with resource_lock(checkout_paths(root), deadline, progress):
                    code = _command(["lake", "--no-build", "build", *targets], root, context, deadline,
                                    diagnostics=diagnostics)
                if code:
                    raise CheckDeferred("target is not ready; compilation or dependency repair can be assigned separately")
            else:
                with resource_lock(checkout_paths(root), deadline, progress), progress.phase("prepare"):
                    prepare_dependencies(root, context, deadline, progress)
                with resource_lock(checkout_paths(root), deadline, progress):
                    if lean_file:
                        code = _build_command(command, root, context, deadline, progress, diagnostics=diagnostics)
                    else:
                        # Up-to-date targets do not consume a host compiler slot.
                        code = _command(["lake", "--no-build", "build", *targets], root, context, deadline,
                                        diagnostics=diagnostics)
                        if code:
                            code = _build_command(command, root, context, deadline, progress, diagnostics=diagnostics)
    except CheckDeferred as exc:
        code, error = 75, str(exc)
        if marker:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.touch()
            except OSError:
                pass
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        code, error = 124, str(exc)
    except KeyboardInterrupt:
        code, error = 130, "interrupted"
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, OSError) and exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            code, error = 75, "Lean build storage is exhausted; retry after capacity is restored"
        else:
            code, error = 1, str(exc)
    finally:
        if previous_signal is not None:
            signal.signal(signal.SIGTERM, previous_signal)
    if code == 1 and diagnostics:
        progress.failure = source_failure("\n".join(diagnostics), root, context)
    if error is None:
        if progress.failure:
            error = progress.failure.get("diagnostic")
        elif diagnostics and code:
            error = diagnostic_summary("\n".join(diagnostics), root, context)
    if code == 0 and marker:
        marker.unlink(missing_ok=True)
    return {**check_details, "returncode": code, "ok": code == 0,
            "status": "passed" if code == 0 else "deferred" if code == 75 else "timed_out" if code == 124
                      else "cancelled" if code == 130 else "failed",
            "duration_seconds": round(time.monotonic() - started, 3), "cache": "local",
            "error": error, "probe": probe, "repeated_deferred": repeated_deferred,
            "failure": progress.failure,
            "timings": {name + "_seconds": round(value, 3) for name, value in progress.seconds.items()}}
