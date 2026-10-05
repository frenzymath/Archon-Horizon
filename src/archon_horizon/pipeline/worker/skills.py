"""Verified immutable instruction bundles shared by resumed provider contexts."""

from __future__ import annotations

import base64
import binascii
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile


MAX_BUNDLE_BYTES = 16 * 1024**2


def _path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid skill bundle path")
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or ".." in path.parts or len(path.parts) > 32 or value == ".":
        raise ValueError("unsafe skill bundle path")
    return path


def materialize_bundle(state_root: Path, digest: str, bundle: dict) -> Path:
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        raise ValueError("invalid skill bundle digest")
    raw = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    if len(raw) > MAX_BUNDLE_BYTES or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("skill bundle digest mismatch or size limit exceeded")
    files, entrypoints = bundle.get("files"), bundle.get("entrypoints")
    if not isinstance(files, dict) or not 1 <= len(files) <= 4096 or not isinstance(entrypoints, list) or not entrypoints:
        raise ValueError("skill bundle requires bounded files and entrypoints")
    contents: dict[PurePosixPath, tuple[bytes, int]] = {}
    for name, item in files.items():
        path = _path(name)
        if not isinstance(item, dict) or set(item) != {"content_base64", "sha256", "executable"} or not isinstance(item["executable"], bool):
            raise ValueError("invalid skill bundle file record")
        try:
            data = base64.b64decode(item["content_base64"], validate=True)
        except (ValueError, TypeError, binascii.Error) as error:
            raise ValueError("invalid skill bundle file encoding") from error
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("skill bundle file digest mismatch")
        contents[path] = (data, 0o555 if item["executable"] else 0o444)
    if any(_path(name) not in contents for name in entrypoints):
        raise ValueError("skill bundle entrypoint is missing")
    if any(parent in contents for path in contents for parent in path.parents):
        raise ValueError("skill bundle file overlaps another path")
    root = state_root / "bundles"
    if root.is_symlink():
        raise ValueError("skill bundle root must not be a symlink")
    root.mkdir(mode=0o700, exist_ok=True)
    target = root / digest
    with (root / ".materialize.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            _verify(target, contents)
            return target
        temporary = Path(tempfile.mkdtemp(prefix=".bundle-", dir=root))
        try:
            for path, (data, mode) in contents.items():
                destination = temporary / path
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with destination.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                destination.chmod(mode)
            for directory, _, _ in os.walk(temporary, topdown=False):
                Path(directory).chmod(0o555)
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            temporary.rename(target)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if temporary.exists():
                for directory, _, _ in os.walk(temporary):
                    Path(directory).chmod(0o700)
                shutil.rmtree(temporary)
    return target


def _verify(target: Path, contents: dict[PurePosixPath, tuple[bytes, int]]) -> None:
    if target.is_symlink() or not target.is_dir():
        raise ValueError("skill bundle cache is not a real directory")
    expected_dirs = {PurePosixPath(".")}
    for path in contents:
        expected_dirs.update(path.parents)
    expected_dirnames = {str(path) for path in expected_dirs}
    seen = set()
    for directory, folders, files in os.walk(target, followlinks=False):
        parent = Path(directory)
        if parent.relative_to(target).as_posix() not in expected_dirnames or parent.stat().st_mode & 0o222:
            raise ValueError("skill bundle cache directory changed")
        for name in folders:
            if (parent / name).is_symlink():
                raise ValueError("skill bundle cache contains a symlink")
        for name in files:
            path = parent / name
            relative = PurePosixPath(path.relative_to(target).as_posix())
            if relative not in contents or path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("skill bundle cache contains an unexpected file")
            data, mode = contents[relative]
            if path.stat().st_size != len(data) or stat.S_IMODE(path.stat().st_mode) != mode or path.read_bytes() != data:
                raise ValueError("skill bundle cached file changed")
            seen.add(relative)
    if seen != set(contents):
        raise ValueError("skill bundle cache is incomplete")
