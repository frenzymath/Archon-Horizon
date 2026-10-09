"""Bounded background search generations over authorized, pinned repositories.

The API resolves SourceSpec from operator-owned repository configuration and
checks project access on every request. One manager owns a cache root; queries
pin generations while the maintenance executor prepares/evicts.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from archon_horizon.search.index import CACHE_VERSION, LeanSearchIndex

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_KEY = re.compile(r"[0-9a-f]{64}\Z")
_MODES = frozenset({"name", "text", "informal", "type", "header"})


@dataclass(frozen=True, slots=True)
class SourceSpec:
    project_id: str
    source_id: str
    url: str
    commit: str
    subdir: str = ""


@dataclass
class _Generation:
    source: SourceSpec
    key: str
    status: Literal["preparing", "ready", "failed"] = "preparing"
    index: LeanSearchIndex | None = None
    memory_bytes: int = 0
    disk_bytes: int = 0
    readers: int = 0
    error: str | None = None


class SearchCapacityError(RuntimeError):
    pass


class SearchStopped(RuntimeError):
    pass


def _tree_bytes(root: Path) -> int:
    total = 0
    for directory, _, files in os.walk(root, followlinks=False):
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except FileNotFoundError:
                continue
    return total


def _retained_bytes(value: Any, seen: set[int] | None = None) -> int:
    """Account Python containers and sparse/NumPy arrays without double counting."""
    seen = seen if seen is not None else set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    size = sys.getsizeof(value)
    if hasattr(value, "nbytes"):
        return max(size, int(value.nbytes))
    if isinstance(value, dict):
        return size + sum(_retained_bytes(k, seen) + _retained_bytes(v, seen) for k, v in value.items())
    if isinstance(value, (list, tuple, set, frozenset)):
        return size + sum(_retained_bytes(item, seen) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return size + sum(_retained_bytes(getattr(value, field.name), seen) for field in fields(value))
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return size + _retained_bytes(vars(value), seen)
    return size


class SearchManager:
    """A credential resolver returns an Authorization value, never a URL token.

    It runs on the maintenance thread and must use operator-resolved credentials;
    values are supplied only to the fetch process environment, not cache keys,
    generation manifests, Git configuration files, or command arguments.
    """
    def __init__(
        self,
        cache_root: Path,
        *,
        allowed_origins: set[str] | frozenset[str] = frozenset(),
        max_workers: int = 1,
        max_pending: int = 4,
        memory_budget_bytes: int = 1024**3,
        disk_budget_bytes: int = 4 * 1024**3,
        max_generation_bytes: int | None = None,
        git_timeout_seconds: float = 180,
        local_source_root: Path | None = None,
        credential_resolver: Callable[[SourceSpec], str | None] | None = None,
    ) -> None:
        if not cache_root.is_absolute() or cache_root == Path("/"):
            raise ValueError("search cache_root must be an explicit absolute directory")
        if min(max_workers, max_pending, memory_budget_bytes, disk_budget_bytes) <= 0:
            raise ValueError("search worker and byte limits must be positive")
        self.root = cache_root.resolve()
        self.allowed_origins = frozenset(origin.rstrip("/") for origin in allowed_origins)
        self.local_source_root = local_source_root.resolve() if local_source_root else None
        self.credential_resolver = credential_resolver
        self.memory_budget_bytes = memory_budget_bytes
        self.disk_budget_bytes = disk_budget_bytes
        self.max_generation_bytes = min(max_generation_bytes or max(1, min(1024**3, disk_budget_bytes // 2)), disk_budget_bytes)
        if self.max_generation_bytes <= 0 or git_timeout_seconds <= 0:
            raise ValueError("generation size and Git timeout must be positive")
        self.git_timeout_seconds = git_timeout_seconds
        self.max_pending = max_pending
        self._lock = threading.RLock()
        self._entries: OrderedDict[str, _Generation] = OrderedDict()
        self._building = 0
        self._reserved: dict[str, int] = {}
        self._evicting: set[str] = set()
        self._closed = False
        self._stopping = threading.Event()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._owner = (self.root / "manager.lock").open("a+b")
        try:
            fcntl.flock(self._owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._owner.close()
            raise RuntimeError("a search manager already owns this cache root") from None
        self.generations = self.root / "generations"
        self.staging = self.root / "staging"
        self.generations.mkdir(exist_ok=True)
        self.staging.mkdir(exist_ok=True)
        # Only a singleton owner can remove incomplete work from an earlier crash.
        for incomplete in self.staging.iterdir():
            if incomplete.is_dir() and not incomplete.is_symlink():
                shutil.rmtree(incomplete)
        self._disk = {item.name: _tree_bytes(item) for item in self.generations.iterdir()
                      if _KEY.fullmatch(item.name) and item.is_dir() and not item.is_symlink()}
        try:
            ranker = importlib.metadata.version("bm25s")
        except importlib.metadata.PackageNotFoundError:
            ranker = "unavailable"
        self.engine_version = f"extractor-{CACHE_VERSION}:bm25s-{ranker}:tokenizer-1"
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="horizon-search")

    def validate_source(self, source: SourceSpec) -> None:
        if not source.project_id or not source.source_id or not _COMMIT.fullmatch(source.commit):
            raise ValueError("search requires project/source identities and a pinned full commit")
        path = PurePosixPath(source.subdir)
        if source.subdir and (path.is_absolute() or ".." in path.parts or "\\" in source.subdir
                              or "\x00" in source.subdir or str(path) != source.subdir):
            raise ValueError("search subdir must be a normalized repository-relative path")
        parsed = urlsplit(source.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("search source URLs cannot contain credentials, query, or fragment")
        if parsed.scheme == "file":
            local = Path(unquote(parsed.path)).resolve()
            if parsed.netloc or self.local_source_root is None or not local.is_relative_to(self.local_source_root):
                raise ValueError("local search source is outside the explicitly configured source root")
            return
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self.allowed_origins or parsed.scheme not in {"https", "http"}:
            raise ValueError("search source origin is not operator-allowlisted")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("remote search source requires HTTPS")

    def generation_key(self, source: SourceSpec) -> str:
        self.validate_source(source)
        content = json.dumps({**asdict(source), "engine": self.engine_version}, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()

    @staticmethod
    def _payload(entry: _Generation) -> dict[str, Any]:
        return {"status": entry.status, "source_id": entry.source.source_id,
                "project_id": entry.source.project_id, "indexed_commit": entry.source.commit if entry.status == "ready" else None,
                "desired_commit": entry.source.commit, "generation": entry.key,
                "subdir": entry.source.subdir, "error": entry.error}

    def prepare(self, source: SourceSpec, *, retry: bool = False) -> dict[str, Any]:
        key = self.generation_key(source)
        with self._lock:
            if self._closed:
                raise RuntimeError("search manager is closed")
            if key in self._evicting:
                return {**self._payload(_Generation(source, key)), "reason": "storage_cleanup"}
            if entry := self._entries.get(key):
                self._entries.move_to_end(key)
                if entry.status != "failed" or not retry:
                    return self._payload(entry)
            if self._building >= self.max_pending:
                return {**self._payload(_Generation(source, key)), "reason": "background_capacity"}
            entry = _Generation(source, key)
            self._entries[key] = entry
            self._building += 1
            self._executor.submit(self._prepare, entry)
            return self._payload(entry)

    def search(self, source: SourceSpec, query: str, *, mode: str = "text", limit: int = 20) -> dict[str, Any]:
        if mode not in _MODES or not 1 <= limit <= 100 or not query.strip() or len(query) > 4096:
            raise ValueError("search needs a nonempty query <=4096 characters, known mode, and limit 1..100")
        state = self.prepare(source)
        if state["status"] != "ready":
            return {**state, "items": []}
        with self.pin(source) as entry:
            if entry is None:
                return {**self.prepare(source), "items": []}
            assert entry.index is not None
            hits = entry.index.query(query, mode=mode, limit=limit)
            return {**self._payload(entry), "mode": mode, "items": [
                {**asdict(hit.declaration), "doc": hit.declaration.doc[:1000],
                 "header": hit.declaration.header[:1000], "score": round(hit.score, 4),
                 "source_id": source.source_id, "commit": source.commit}
                for hit in hits
            ]}

    @contextmanager
    def pin(self, source: SourceSpec) -> Iterator[_Generation | None]:
        key = self.generation_key(source)
        with self._lock:
            entry = self._entries.get(key)
            if entry and entry.status == "ready":
                entry.readers += 1
                self._entries.move_to_end(key)
            else:
                entry = None
        try:
            yield entry
        finally:
            if entry:
                with self._lock:
                    entry.readers -= 1

    def _evict(self, *, memory_needed: int = 0, disk_needed: int = 0, exclude: str = "") -> list[str]:
        """Called only by the maintenance executor under its short metadata lock."""
        memory = sum(entry.memory_bytes for entry in self._entries.values())
        disk = sum(self._disk.values()) + sum(self._reserved.values())
        retired: list[str] = []
        unloaded: set[str] = set()
        keys = list(self._disk) + [key for key in self._entries if key not in self._disk]
        recency = {key: position for position, key in enumerate(self._entries)}
        keys.sort(key=lambda key: recency.get(key, -1))
        for key in keys:
            if memory + memory_needed <= self.memory_budget_bytes and disk + disk_needed <= self.disk_budget_bytes:
                break
            entry = self._entries.get(key)
            if key == exclude or key in self._evicting or (entry and (entry.readers or entry.status == "preparing")):
                continue
            if entry and entry.memory_bytes and memory + memory_needed > self.memory_budget_bytes:
                memory -= entry.memory_bytes
                unloaded.add(key)
            if self._disk.get(key, 0) and disk + disk_needed > self.disk_budget_bytes:
                disk -= self._disk[key]
                retired.append(key)
                if entry and key not in unloaded:
                    memory -= entry.memory_bytes
                    unloaded.add(key)
        if memory + memory_needed > self.memory_budget_bytes or disk + disk_needed > self.disk_budget_bytes:
            raise SearchCapacityError("search budget is exhausted or active readers pin the remaining generations")
        for key in unloaded:
            self._entries.pop(key, None)
        for key in retired:
            self._evicting.add(key)
        return retired

    def _remove_retired(self, keys: list[str]) -> None:
        try:
            for key in keys:
                shutil.rmtree(self.generations / key, ignore_errors=False)
                with self._lock:
                    self._disk.pop(key, None)
        finally:
            # A failed deletion still occupies disk, but must not permanently
            # fence preparation or a later cleanup retry for that generation.
            with self._lock:
                self._evicting.difference_update(keys)

    def _prepare(self, entry: _Generation) -> None:
        stage = self.staging / f"{entry.key}-{uuid4().hex}"
        target = self.generations / entry.key
        try:
            self._check_stopping()
            index = self._load_generation(entry, target)
            self._check_stopping()
            if index is None:
                if target.exists():
                    shutil.rmtree(target)
                with self._lock:
                    self._disk.pop(entry.key, None)
                    retired = self._evict(disk_needed=self.max_generation_bytes, exclude=entry.key)
                    self._reserved[entry.key] = self.max_generation_bytes
                self._remove_retired(retired)
                self._check_stopping()
                stage.mkdir()
                repo = stage / "repository"
                self._clone(entry.source, repo, stage)
                source_root = (repo / entry.source.subdir).resolve()
                if not source_root.is_relative_to(repo.resolve()) or not source_root.is_dir():
                    raise ValueError("search subdir is missing or leaves the pinned repository")
                for lean in source_root.rglob("*.lean"):
                    self._check_stopping()
                    if not lean.resolve().is_relative_to(repo.resolve()):
                        raise ValueError("Lean source symlink leaves the pinned repository")
                index = LeanSearchIndex.build({entry.source.source_id: source_root}, workspace_root=repo,
                                             check_cancelled=self._check_stopping)
                index.save_cache(stage / "index", entry.key, check_cancelled=self._check_stopping)
                self._check_stopping()
                (stage / "generation.json").write_text(json.dumps({"source": asdict(entry.source), "engine": self.engine_version, "key": entry.key}))
                disk_bytes = _tree_bytes(stage)
                if disk_bytes > self.max_generation_bytes:
                    raise SearchCapacityError("search generation exceeds its disk allowance")
                self._check_stopping()
                stage.replace(target)
                index = LeanSearchIndex.load_cache(target / "index", entry.key)
                if index is None:
                    raise RuntimeError("new search generation could not be reopened")
            self._check_stopping()
            index.search_name("horizon_index_warmup", limit=1)
            self._check_stopping()
            index.search_header("horizon_index_warmup", limit=1)
            self._check_stopping()
            memory_bytes = _retained_bytes(index)
            disk_bytes = _tree_bytes(target)
            with self._lock:
                self._reserved.pop(entry.key, None)
                self._disk[entry.key] = disk_bytes
                retired = self._evict(memory_needed=memory_bytes, exclude=entry.key)
                entry.index = index
                entry.memory_bytes = memory_bytes
                entry.disk_bytes = disk_bytes
            self._remove_retired(retired)
            with self._lock:
                entry.status = "ready"
        except SearchStopped:
            # Shutdown is not cache corruption. Preserve any fully published
            # generation, including one reopened from a previous process.
            with self._lock:
                entry.error = "Search preparation cancelled during shutdown"
        except Exception as exc:
            with self._lock:
                entry.error = str(exc)[:1000] or type(exc).__name__
                self._disk.pop(entry.key, None)
            shutil.rmtree(target, ignore_errors=True)
        finally:
            shutil.rmtree(stage, ignore_errors=True)
            with self._lock:
                self._building -= 1
                self._reserved.pop(entry.key, None)
                if entry.error:
                    entry.status = "failed"
                    entry.index = None
                    entry.memory_bytes = 0
                    entry.disk_bytes = 0
                # Failed requests consume no corpus memory and retain only bounded diagnostics.
                failed = [key for key, value in self._entries.items() if value.status == "failed"]
                for key in failed[:-64]:
                    del self._entries[key]

    def _load_generation(self, entry: _Generation, target: Path) -> LeanSearchIndex | None:
        if not target.is_dir() or target.is_symlink():
            return None
        try:
            manifest = json.loads((target / "generation.json").read_text())
            if manifest != {"source": asdict(entry.source), "engine": self.engine_version, "key": entry.key}:
                return None
            repo = target / "repository"
            head = self._git(["rev-parse", "HEAD"], repo, target).strip()
            if head != entry.source.commit or self._git(["status", "--porcelain", "--untracked-files=all"], repo, target).strip():
                return None
            return LeanSearchIndex.load_cache(target / "index", entry.key)
        except SearchStopped:
            raise
        except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
            return None

    def _clone(self, source: SourceSpec, repo: Path, stage: Path) -> None:
        repo.mkdir()
        self._git(["init", "--quiet"], repo, stage)
        self._git(["remote", "add", "origin", source.url], repo, stage)
        authorization = self.credential_resolver(source) if self.credential_resolver else None
        if authorization is not None and (not isinstance(authorization, str) or not authorization.strip() or "\n" in authorization or "\r" in authorization):
            raise ValueError("search credential resolver must return an Authorization header value without newlines")
        self._git(["fetch", "--quiet", "--depth=1", "--no-tags", "origin", source.commit], repo, stage,
                  authorization=authorization)
        self._git(["checkout", "--quiet", "--detach", "FETCH_HEAD"], repo, stage)
        if self._git(["rev-parse", "HEAD"], repo, stage).strip() != source.commit:
            raise RuntimeError("fetched repository did not match the pinned source commit")

    def _git(self, arguments: list[str], cwd: Path, monitored_root: Path, *, authorization: str | None = None) -> str:
        self._check_stopping()
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
        if authorization:
            environment.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                               GIT_CONFIG_VALUE_0=f"Authorization: {authorization}")
        command = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "credential.helper=",
                   "-c", "http.followRedirects=false", "-c", "protocol.allow=never",
                   "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=always"]
        if self.local_source_root:
            command.extend(["-c", "protocol.file.allow=always"])
        deadline = time.monotonic() + self.git_timeout_seconds
        # Diagnostic output lives on the same budgeted disk, not in an unbounded pipe.
        log = monitored_root / "git-operation.log"
        with log.open("w+b") as output:
            process = subprocess.Popen([*command, *arguments], cwd=cwd, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            try:
                while process.poll() is None:
                    self._check_stopping()
                    if time.monotonic() > deadline or _tree_bytes(monitored_root) > self.max_generation_bytes:
                        raise SearchCapacityError("Git source preparation exceeded its time or disk allowance")
                    self._stopping.wait(0.05)
                self._check_stopping()
                if _tree_bytes(monitored_root) > self.max_generation_bytes:
                    raise SearchCapacityError("Git source preparation exceeded its disk allowance")
                output.seek(0)
                result = output.read(65536).decode("utf-8", errors="replace")
                if authorization:
                    result = result.replace(authorization, "[redacted]").replace(authorization.split()[-1], "[redacted]")
                    output.seek(0)
                    output.write(result.encode())
                    output.truncate()
                if process.returncode:
                    raise RuntimeError(f"Git source preparation failed: {result[-2000:]}")
                return result
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()

    def _check_stopping(self) -> None:
        if self._stopping.is_set():
            raise SearchStopped("Search manager is shutting down")

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"memory_bytes": sum(item.memory_bytes for item in self._entries.values()),
                    "disk_bytes": sum(self._disk.values()), "preparing": self._building,
                    "reserved_disk_bytes": sum(self._reserved.values()),
                    "pinned_generations": sum(item.readers > 0 for item in self._entries.values()),
                    "ready_generations": sum(item.status == "ready" for item in self._entries.values())}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
        self._executor.shutdown(wait=True, cancel_futures=False)
        fcntl.flock(self._owner, fcntl.LOCK_UN)
        self._owner.close()

    def __enter__(self) -> SearchManager:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
