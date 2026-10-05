"""Bounded, disposable workspace BibTeX cache with authenticated revalidation.

This module is worker-portable: stdlib and HTTPX only. It contacts the configured
Horizon endpoint, never publication URLs stored in bibliographic metadata.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class ReferenceCacheError(ValueError):
    pass


@dataclass(frozen=True)
class CachedReference:
    reference_id: UUID
    revision: int
    sha256: str
    content: bytes
    path: Path

    @property
    def text(self):
        return self.content.decode("utf-8")


class ReferenceCache:
    def __init__(self, workspace: Path, base_url: str, token: str, *, max_bytes: int = 8 * 1024**2,
                 max_entry_bytes: int = 256 * 1024, max_entries: int = 512, client: httpx.Client | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ReferenceCacheError("invalid Horizon API URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
            raise ReferenceCacheError("remote reference cache requests require HTTPS")
        if not token or any(character in token for character in "\r\n"):
            raise ReferenceCacheError("reference cache requires an API credential")
        if max_entry_bytes < 1 or max_bytes < max_entry_bytes + 4096 or max_entries < 1:
            raise ReferenceCacheError("invalid reference cache limits")
        self.workspace = Path(workspace).resolve(strict=True)
        self.root = self.workspace / ".horizon" / "reference-cache"
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.scope = hashlib.sha256((self.base_url + "\0" + token).encode()).hexdigest()
        self.max_bytes, self.max_entry_bytes, self.max_entries = max_bytes, max_entry_bytes, max_entries
        self._owned_client = client is None
        self.client = client or httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False,
            trust_env=False, limits=httpx.Limits(max_connections=2, max_keepalive_connections=1))
        self._ensure_directory()

    def _ensure_directory(self):
        for directory in (self.root.parent, self.root):
            if directory.is_symlink():
                raise ReferenceCacheError("workspace reference cache cannot use symlink directories")
            directory.mkdir(mode=0o700, exist_ok=True)
            if not directory.is_dir() or not directory.resolve().is_relative_to(self.workspace):
                raise ReferenceCacheError("reference cache must remain inside the workspace")

    @contextmanager
    def _lock(self):
        self._ensure_directory()
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _key(self, reference_id: UUID):
        return hashlib.sha256((self.scope + ":" + str(reference_id)).encode()).hexdigest()

    def _paths(self, key):
        return self.root / (key + ".json"), self.root / (key + ".bib")

    @staticmethod
    def _etag(value):
        return value if isinstance(value, str) and re.fullmatch(r'(?:W/)?"[^"\r\n]{1,256}"', value) else None

    def _read_file(self, path, maximum):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > maximum:
                raise ReferenceCacheError("cached reference exceeds its size limit")
            value = handle.read(maximum + 1)
            if len(value) > maximum:
                raise ReferenceCacheError("cached reference exceeds its size limit")
            return value

    def _load(self, reference_id):
        key = self._key(reference_id)
        metadata_path, content_path = self._paths(key)
        try:
            metadata = json.loads(self._read_file(metadata_path, 4096))
            content = self._read_file(content_path, self.max_entry_bytes)
            if (metadata.get("schema_version") != 1 or metadata.get("scope") != self.scope
                    or metadata.get("reference_id") != str(reference_id)
                    or type(metadata.get("revision")) is not int or metadata["revision"] < 1
                    or not self._etag(metadata.get("etag")) or metadata.get("size_bytes") != len(content)
                    or hashlib.sha256(content).hexdigest() != metadata.get("sha256")):
                raise ReferenceCacheError("cached reference metadata or checksum differs")
            content.decode("utf-8")
            return metadata, CachedReference(reference_id, metadata["revision"], metadata["sha256"], content, content_path)
        except (OSError, ValueError, TypeError, AttributeError):
            for path in (metadata_path, content_path):
                path.unlink(missing_ok=True)
            return None

    def _replace(self, path, content):
        fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _prune(self, keep):
        entries = {}
        for path in self.root.iterdir():
            if path.name.startswith(".incoming-"):
                path.unlink(missing_ok=True)
                continue
            match = re.fullmatch(r"([0-9a-f]{64})\.(json|bib)", path.name)
            if not match:
                continue
            if path.is_symlink():
                path.unlink()
                continue
            entries.setdefault(match[1], []).append(path)
        complete = []
        for key, paths in entries.items():
            if len(paths) != 2:
                for path in paths:
                    path.unlink(missing_ok=True)
                continue
            complete.append((max(path.stat().st_mtime_ns for path in paths), key,
                             sum(path.stat().st_size for path in paths), paths))
        count, size = len(complete), sum(entry[2] for entry in complete)
        for _, key, amount, paths in sorted(complete):
            if count <= self.max_entries and size <= self.max_bytes:
                break
            if key == keep:
                continue
            for path in paths:
                path.unlink(missing_ok=True)
            count -= 1
            size -= amount

    def get(self, reference_id: UUID | str) -> CachedReference:
        reference_id = UUID(str(reference_id))
        key = self._key(reference_id)
        with self._lock():
            cached = self._load(reference_id)
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/x-bibtex"}
        if cached:
            headers["If-None-Match"] = cached[0]["etag"]
        url = self.base_url + f"/api/v3/references/{reference_id}/bibtex"
        # An inconsistent 304 is retried once without the conditional validator.
        for _ in range(2):
            with self.client.stream("GET", url, headers=headers, timeout=httpx.Timeout(10, connect=5), follow_redirects=False) as response:
                if response.status_code in (401, 403, 404, 410):
                    with self._lock():
                        for path in self._paths(key):
                            path.unlink(missing_ok=True)
                if response.status_code == 304:
                    revision = response.headers.get("X-Horizon-Reference-Revision")
                    if cached and revision == str(cached[1].revision) and response.headers.get("etag") == cached[0]["etag"]:
                        with self._lock():
                            # Another local process may have evicted this entry while
                            # the network request was pending. Restore verified bytes.
                            return self._store(reference_id, cached[1].content, cached[1].revision, cached[0]["etag"])
                    headers.pop("If-None-Match", None)
                    cached = None
                    continue
                response.raise_for_status()
                if response.status_code != 200:
                    raise ReferenceCacheError("reference endpoint did not return a complete export")
                try:
                    revision = int(response.headers["X-Horizon-Reference-Revision"])
                except (KeyError, ValueError) as error:
                    raise ReferenceCacheError("reference response lacks its revision") from error
                etag = self._etag(response.headers.get("etag"))
                if revision < 1 or etag is None:
                    raise ReferenceCacheError("reference response lacks valid revision or ETag")
                content = bytearray()
                for chunk in response.iter_bytes(chunk_size=min(65536, self.max_entry_bytes + 1)):
                    content.extend(chunk)
                    if len(content) > self.max_entry_bytes:
                        raise ReferenceCacheError("reference export exceeds the cache entry limit")
                try:
                    bytes(content).decode("utf-8")
                except UnicodeDecodeError as error:
                    raise ReferenceCacheError("reference export is not UTF-8") from error
                with self._lock():
                    return self._store(reference_id, bytes(content), revision, etag)
        raise ReferenceCacheError("reference server returned inconsistent conditional responses")

    def _store(self, reference_id, content, revision, etag):
        existing = self._load(reference_id)
        if existing and existing[1].revision > revision:
            return existing[1]
        digest = hashlib.sha256(content).hexdigest()
        if existing and existing[1].revision == revision and existing[1].sha256 != digest:
            raise ReferenceCacheError("reference body changed without a revision change")
        key = self._key(reference_id)
        metadata_path, content_path = self._paths(key)
        metadata = {"schema_version": 1, "scope": self.scope, "reference_id": str(reference_id),
                    "revision": revision, "sha256": digest, "size_bytes": len(content), "etag": etag}
        if not existing or existing[1].revision != revision or existing[1].sha256 != digest or existing[0]["etag"] != etag:
            self._replace(content_path, content)
            self._replace(metadata_path, json.dumps(metadata, sort_keys=True).encode())
        else:
            os.utime(metadata_path, None, follow_symlinks=False)
        self._prune(key)
        return CachedReference(reference_id, revision, digest, content, content_path)

    def close(self):
        if self._owned_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
