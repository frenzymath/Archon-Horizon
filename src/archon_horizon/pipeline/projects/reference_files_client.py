"""Worker-portable source-file transfer with durable upload replay and checksums.

Only stdlib and HTTPX are required. Transfers contact the configured Horizon
origin; bibliographic publication URLs never receive execution credentials.
"""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx

# Matches the maximum blob accepted by the default server. The server's
# installation-specific bound can be discovered by listing reference files.
MAX_FILE_BYTES = 64 * 1024**2


def upload_snapshot(client, method, path, body):
    """Validate a journal descriptor and its retained bytes before each replay."""
    if method != "POST" or not re.fullmatch(r"/api/v3/references/[0-9a-f-]{36}/files", urlsplit(path).path):
        raise ValueError("file descriptors are only valid for reference uploads")
    descriptor = body["_horizon_file_upload"]
    digest, size = descriptor.get("sha256"), descriptor.get("size_bytes")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or type(size) is not int or not 0 < size <= MAX_FILE_BYTES:
        raise ValueError("invalid file upload journal descriptor")
    root = client.path.parent / "reference-uploads"
    path = root / digest
    if root.is_symlink() or path.is_symlink() or not path.is_file() or path.stat().st_size != size:
        raise ValueError("retained upload is missing or unsafe; reconcile the original intent")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != digest:
        raise ValueError("retained upload checksum differs; reconcile the original intent")
    return path


def upload_file(client, reference_id, source: Path, *, description="", source_url=None):
    """Protect staged files from another uploader's completed-file cleanup."""
    root = client.path.parent / "reference-uploads"
    if root.is_symlink():
        raise ValueError("upload journal cannot use a symlink directory")
    root.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _upload_file(client, reference_id, source, description=description, source_url=source_url)
    finally:
        os.close(fd)


def _upload_file(client, reference_id, source: Path, *, description="", source_url=None):
    """Journal an exact private file snapshot before sending a binary upload."""
    reference_id = UUID(str(reference_id))
    if source.is_symlink() or not source.is_file():
        raise ValueError("reference upload requires a regular, non-symlink file")
    root = client.path.parent / "reference-uploads"
    if root.is_symlink():
        raise ValueError("upload journal cannot use a symlink directory")
    root.mkdir(mode=0o700, exist_ok=True)
    prune_uploads(client)
    retained_bytes = sum(p.stat().st_size for p in root.iterdir() if p.is_file() and re.fullmatch(r"[0-9a-f]{64}", p.name))
    # This only bounds unresolved upload snapshots. Successful uploads release
    # their copies, so a literature review can store arbitrarily many papers.
    spool_limit = max(2 * MAX_FILE_BYTES, client.max_journal_bytes)
    fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=root)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            while chunk := input_file.read(65536):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError("reference file exceeds 64 MiB")
                if size + retained_bytes > spool_limit:
                    raise ValueError("reference upload journal is full; reconcile pending transfers first")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if not size:
            raise ValueError("reference file is empty")
        target = root / digest.hexdigest()
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
        directory = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        query = {"filename": source.name, "description": description}
        if source_url:
            query["source_url"] = source_url
        body = {"_horizon_file_upload": {"sha256": digest.hexdigest(), "size_bytes": size}}
        result = client.request("POST", f"/api/v3/references/{reference_id}/files?" + urlencode(query), body)
        prune_uploads(client)
        return result
    finally:
        Path(temporary).unlink(missing_ok=True)


def prune_uploads(client):
    """Keep pending/rejected transfer bytes; completed snapshots are disposable."""
    with client.connect() as db:
        descriptors = [json.loads(row[0]) for row in db.execute("SELECT body FROM intent WHERE status IN ('pending','rejected')")]
    keep = {body["_horizon_file_upload"]["sha256"] for body in descriptors if isinstance(body, dict) and "_horizon_file_upload" in body}
    root = client.path.parent / "reference-uploads"
    if root.is_dir() and not root.is_symlink():
        for path in root.iterdir():
            if re.fullmatch(r"[0-9a-f]{64}", path.name) and path.name not in keep:
                path.unlink(missing_ok=True)


def download_file(client, reference_id, file_id, destination: Path):
    """Stream authenticated bytes, verify identity, then install without clobbering."""
    reference_id, file_id = UUID(str(reference_id)), UUID(str(file_id))
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise ValueError("download needs an unused absolute output filename")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".reference-", dir=destination.parent)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as output, client.client.stream("GET", client.base_url +
                f"/api/v3/references/{reference_id}/files/{file_id}/content",
                headers={"Authorization": "Bearer " + client.token}, timeout=httpx.Timeout(120, connect=5), follow_redirects=False) as response:
            response.raise_for_status()
            expected = response.headers.get("X-Content-SHA256", "")
            if response.status_code != 200 or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("reference download lacks a complete checksum identity")
            for chunk in response.iter_bytes(65536):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise ValueError("reference download exceeds 64 MiB")
                output.write(chunk)
                digest.update(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise ValueError("reference download checksum differs")
        os.link(temporary, destination)
        return {"path": str(destination), "sha256": expected, "size_bytes": size}
    finally:
        Path(temporary).unlink(missing_ok=True)
