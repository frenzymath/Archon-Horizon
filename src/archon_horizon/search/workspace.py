"""Search a worker checkout and fetched Lake packages without a workspace manifest."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Mapping

from .index import CACHE_VERSION, LeanSearchIndex, _iter_source_files

# Walk authored sources separately from private state and generated/vendor trees.
# Lake's manifest supplies dependency roots explicitly, including .lake/packages.
_SKIP = {".git", ".lake", "_lake", ".horizon", ".archon-horizon", "node_modules", "agent-library"}
_PROJECT_FILES = {"lakefile.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"}
# Include the index compatibility revision in fingerprints as well as meta.json.
_INDEX_VERSION = CACHE_VERSION


def resolve_source_roots(root: Path, source_roots: Mapping[str, Path] | None = None) -> dict[str, Path]:
    """Include nested Lake projects and their available local dependencies.

    Project directory names are stable across worker session checkouts. Explicit
    source roots retain their supplied names and do not trigger discovery.
    """
    root = root.expanduser().resolve()
    roots = {str(name): Path(path).expanduser().resolve() for name, path in source_roots.items()} if source_roots is not None else {root.name: root}
    for name, path in roots.items():
        if not path.is_dir():
            raise FileNotFoundError(f"Lean source directory {name!r} does not exist: {path}")
    if source_roots is not None:
        return roots
    candidates = {root: root.name}
    pending = [root]
    visited: set[Path] = set()

    def include(path: Path, name: str | None = None, *, prefer_name: bool = False) -> None:
        resolved = path.resolve()
        if not resolved.is_dir():
            return
        if resolved not in candidates:
            candidates[resolved] = name or path.name
            pending.append(resolved)
        elif prefer_name:
            candidates[resolved] = name or path.name

    while pending:
        for directory, folders, files in os.walk(pending.pop()):
            project = Path(directory).resolve()
            if project in visited:
                folders[:] = []
                continue
            visited.add(project)
            if _PROJECT_FILES.intersection(files):
                authored = project.is_relative_to(root) and not _SKIP.intersection(project.relative_to(root).parts)
                include(project, prefer_name=authored)
            manifest = _read_manifest(project) if "lake-manifest.json" in files else {}
            package_dir = manifest.get("packagesDir", ".lake/packages")
            if isinstance(package_dir, str):
                packages = project / package_dir
                if packages.is_dir():
                    for package in sorted(packages.iterdir()):
                        include(package, package.name)
            dependencies = manifest.get("packages", [])
            if isinstance(dependencies, list):
                for package in dependencies:
                    if not isinstance(package, dict) or package.get("type") != "path":
                        continue
                    directory = package.get("dir")
                    if isinstance(directory, str) and directory:
                        include(project / directory, prefer_name=True)
            folders[:] = sorted(name for name in folders if name not in _SKIP)

    roots = {}
    for path, name in sorted(candidates.items(), key=lambda item: str(item[0])):
        key = name
        if key in roots:
            key = f"{os.path.relpath(path, root)}:{name}"
        roots[key] = path
    return roots


def _read_manifest(project: Path) -> dict:
    try:
        manifest = json.loads((project / "lake-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


def _fingerprint(roots: Mapping[str, Path]) -> str:
    """Detect ordinary source changes cheaply using names, paths, mtimes and sizes.

    This avoids hashing every source file before each search, but is not a content
    integrity check. Use ``reindex=True`` after changes that preserve file metadata.
    """
    digest = hashlib.sha256(str(_INDEX_VERSION).encode())
    for name, root in sorted(roots.items()):
        digest.update(json.dumps([name, str(root)]).encode())
    for library, path in _iter_source_files(roots):
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        digest.update(json.dumps([library, str(path), stat.st_mtime_ns, stat.st_size]).encode())
    return digest.hexdigest()


def load_or_build_index(root: Path, source_roots: Mapping[str, Path] | None = None, *,
                        reindex: bool = False, cache_dir: Path | None = None) -> LeanSearchIndex:
    """Reuse or rebuild a workspace index under a per-cache interprocess lock.

    By default, caches live under XDG_CACHE_HOME (or ~/.cache), keeping generated
    data outside the checkout. A rebuild stages all files beside the destination
    before replacement, so cooperating callers do not load a partial write.
    """
    root = root.expanduser().resolve()
    roots = resolve_source_roots(root, source_roots)
    cache_home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    cache = (cache_dir or cache_home / "archon-horizon" / "lean-search").expanduser()
    cache.mkdir(parents=True, exist_ok=True)
    # The same sources can be displayed relative to different workspaces. Include
    # that display root in the key so cached file paths belong to this caller.
    identity = {"workspace_root": str(root), "source_roots": {name: str(path) for name, path in sorted(roots.items())}}
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    dest = cache / key
    # Keep the lock outside the replaceable directory so rebuilding cannot
    # replace the lock inode and allow a second builder into the critical section.
    with (cache / f"{key}.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        fingerprint = _fingerprint(roots)
        if not reindex:
            loaded = LeanSearchIndex.load_cache(dest, fingerprint)
            if loaded is not None:
                return loaded
        index = LeanSearchIndex.build(roots, workspace_root=root)
        staging = Path(tempfile.mkdtemp(prefix=key + ".", dir=cache))
        try:
            index.save_cache(staging, fingerprint)
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(staging, dest)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # Staging has moved: reopen against dest so lazy retrievers use the
        # published location rather than a directory that no longer exists.
        return LeanSearchIndex.load_cache(dest, fingerprint) or index
