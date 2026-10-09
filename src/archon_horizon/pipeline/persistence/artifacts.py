"""Content-addressed evidence storage with atomic, crash-durable installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from ..errors import DomainError


class ArtifactStore:
    def __init__(self, root: Path, *, max_blob_bytes: int = 64 * 1024**2):
        self.root = root
        self.max_blob_bytes = max_blob_bytes

    def path(self, digest: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid SHA-256 identity")
        return self.root / digest[:2] / digest[2:]

    def put(self, content: bytes, media_type: str = "application/octet-stream") -> dict:
        if len(content) > self.max_blob_bytes:
            raise DomainError("artifact_too_large", "Artifact exceeds the configured upload limit", 413)
        digest = hashlib.sha256(content).hexdigest()
        target = self.path(digest)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if target.is_file():
            self.read(digest, len(content))
            return {"sha256": digest, "size_bytes": len(content), "media_type": media_type}
        fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            # Link instead of replace: concurrent identical uploads do not overwrite evidence.
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(digest, len(content))
            directory_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return {"sha256": digest, "size_bytes": len(content), "media_type": media_type}

    def put_json(self, value) -> dict:
        return self.put(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=True).encode(), "application/json")

    def put_file(self, source: Path, media_type: str = "application/octet-stream") -> dict:
        """Install a staged upload without holding a whole paper in RAM.

        Copy to a temporary file on the artifact filesystem before atomically
        linking it. Scratch and the artifact root may be different filesystems.
        """
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.root)
        digest, size = hashlib.sha256(), 0
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                while chunk := input_file.read(65536):
                    size += len(chunk)
                    if size > self.max_blob_bytes:
                        raise DomainError("artifact_too_large", "Artifact exceeds the configured upload limit", 413)
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            identity = digest.hexdigest()
            target = self.path(identity)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.verified_path(identity, size)
            directory = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return {"sha256": identity, "size_bytes": size, "media_type": media_type}
        finally:
            Path(temporary).unlink(missing_ok=True)

    def verified_path(self, digest: str, expected_size: int) -> Path:
        """Verify immutable bytes in chunks before streaming an authenticated read."""
        target = self.path(digest)
        if not target.is_file():
            raise DomainError("artifact_missing", "Durable artifact is unavailable", 503)
        size, actual = 0, hashlib.sha256()
        with target.open("rb") as source:
            while chunk := source.read(65536):
                size += len(chunk)
                if size > self.max_blob_bytes:
                    raise DomainError("artifact_corrupt", "Artifact exceeds its size limit", 503)
                actual.update(chunk)
        if size != expected_size or actual.hexdigest() != digest:
            raise DomainError("artifact_corrupt", "Artifact checksum or size verification failed", 503)
        return target

    def read(self, digest: str, expected_size: int | None = None) -> bytes:
        target = self.path(digest)
        if not target.is_file():
            raise DomainError("artifact_missing", "Durable artifact is unavailable", 503)
        size = target.stat().st_size
        if size > self.max_blob_bytes or (expected_size is not None and size != expected_size):
            raise DomainError("artifact_corrupt", "Artifact size does not match its identity", 503)
        content = target.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise DomainError("artifact_corrupt", "Artifact checksum verification failed", 503)
        return content
