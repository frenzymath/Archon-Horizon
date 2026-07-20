"""Wire the search index to a workspace: resolve source roots, cache, refresh.

Source roots come from two places: every configured project (its own ``.lean``)
and every entry under ``external_libraries`` (resolved to checked-out sources,
typically ``<project>/.lake/packages/<lib>``). The built index is cached as
JSONL and only rebuilt when the on-disk sources change.
"""

from __future__ import annotations

import json
from pathlib import Path

from archon_horizon.config.manifest import library_package_names
from archon_horizon.config.schema import WorkspaceConfig
from archon_horizon.log import log

from .index import LeanSearchIndex, _iter_lean_files

_CACHE_DIR = "search"


def resolve_source_roots(root: Path, cfg: WorkspaceConfig) -> dict[str, Path]:
    """Map ``library/project name -> source dir`` for everything to index.

    Projects contribute their own tree; external libraries resolve to an
    explicit ``path``, else the first ``**/.lake/packages/<name>`` found under
    the workspace. Libraries with no checked-out sources are skipped (with a
    note) since there is nothing to index until ``lake`` has fetched them.
    """
    roots: dict[str, Path] = {}

    for name, pc in cfg.projects.items():
        proj = Path(pc.path)
        proj = proj if proj.is_absolute() else root / proj
        if proj.exists():
            roots[name] = proj

    # Package dirs matched case-insensitively by basename. Configured projects'
    # own ``.lake/packages`` come first: that scan is cheap and deterministic,
    # and — unlike the recursive glob, which does not follow symlinks — it works
    # when a project (or its checkout) is symlinked to a shared copy.
    package_dirs: dict[str, Path] = {}

    def _note(pkg: Path) -> None:
        if pkg.is_dir():  # follows symlinks: shared checkouts are often links
            package_dirs.setdefault(pkg.name.lower(), pkg)

    for proj in roots.values():
        packages = proj / ".lake" / "packages"
        if packages.is_dir():
            for pkg in sorted(packages.iterdir()):
                _note(pkg)

    # Fall back to a whole-workspace sweep only if something is still missing
    # (a lake project nested below a configured path, say) — it walks every
    # tree under the root, which is slow on large workspaces.
    unresolved = any(
        not (library_package_names(lib.name) & set(package_dirs))
        for lib in cfg.external_libraries
        if not lib.path
    )
    if unresolved:
        for pkg in root.glob("**/.lake/packages/*"):
            _note(pkg)

    for lib in cfg.external_libraries:
        if lib.path:
            explicit = Path(lib.path)
            explicit = explicit if explicit.is_absolute() else root / explicit
            if explicit.exists():
                roots[lib.name] = explicit
            else:
                log.warn(f"external library {lib.name!r}: path {lib.path!r} does not exist; skipping.")
            continue
        found = next((package_dirs[n] for n in library_package_names(lib.name) if n in package_dirs), None)
        if found is not None:
            roots[lib.name] = found
        else:
            log.info(
                f"external library {lib.name!r}: no checked-out sources found "
                f"(expected under .lake/packages); run `lake build` to fetch it, then `horizon search --reindex`."
            )
    return roots


# Bump when the declaration EXTRACTION logic changes, so stale on-disk caches
# (built by an older extractor) are rebuilt automatically instead of serving
# e.g. signatures truncated at an inner ``:=``.
_INDEX_VERSION = 2


def _fingerprint(roots: dict[str, Path]) -> dict:
    """Cheap change-signature: per root, file count and newest mtime — plus the
    extractor version, so a logic change invalidates the cache."""
    sig: dict[str, object] = {"__index_version__": _INDEX_VERSION}
    for name, root in sorted(roots.items()):
        count = 0
        newest = 0.0
        for path in _iter_lean_files(root):
            count += 1
            try:
                newest = max(newest, path.stat().st_mtime)
            except OSError:
                continue
        sig[name] = [count, newest]
    return sig


def load_or_build_index(
    root: Path, cfg: WorkspaceConfig, *, reindex: bool = False
) -> LeanSearchIndex:
    """Return a ready index, rebuilding the JSONL cache only when sources changed."""
    cache_dir = root / cfg.state_dir / _CACHE_DIR
    index_path = cache_dir / "index.jsonl"
    meta_path = cache_dir / "meta.json"

    roots = resolve_source_roots(root, cfg)
    fingerprint = _fingerprint(roots)

    if not reindex and index_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if meta.get("fingerprint") == fingerprint:
            return LeanSearchIndex.from_jsonl(index_path.read_text("utf-8"))

    index = LeanSearchIndex.build(roots, workspace_root=root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    index_path.write_text(index.to_jsonl() + "\n", "utf-8")
    meta_path.write_text(
        json.dumps({
            "fingerprint": fingerprint,
            "roots": {k: str(v) for k, v in roots.items()},
            "declarations": len(index.declarations),
        }, indent=2),
        "utf-8",
    )
    return index
