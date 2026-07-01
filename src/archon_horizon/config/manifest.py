"""Read Lake's ``lake-manifest.json`` to learn which revs projects actually pin.

The configured ``external_libraries`` declare what the workspace *intends*; the
real pin lives in each project's ``lake-manifest.json``. These helpers scan the
workspace for those manifests so the loader can warn on drift and ``init`` can
propose a sensible default rev — without shelling out to ``lake``.
"""

from __future__ import annotations

import json
from pathlib import Path

# Aliases so a configured library name matches the package name Lake records.
_PACKAGE_ALIASES: dict[str, set[str]] = {
    "mathlib": {"mathlib", "mathlib4"},
    "batteries": {"batteries", "std4", "std"},
    "qq": {"qq", "quote4"},
    "importgraph": {"importgraph", "import-graph"},
    "proofwidgets": {"proofwidgets", "proofwidgets4"},
}


def library_package_names(library: str) -> set[str]:
    """Lake package names a configured library could appear under (lowercase)."""
    key = library.lower()
    # A bare "owner/repo" name pins on the repo's last path segment.
    short = key.rsplit("/", 1)[-1]
    return _PACKAGE_ALIASES.get(short, {short}) | {key, short}


def _iter_manifests(root: Path):
    for manifest in root.glob("**/lake-manifest.json"):
        rel = manifest.relative_to(root)
        # Skip hidden dirs (the Horizon state dir, .git, …) but not .lake itself.
        if any(part.startswith(".") and part != ".lake" for part in rel.parts):
            continue
        try:
            data = json.loads(manifest.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        yield manifest, data


def find_package_revs(root: Path, library: str) -> dict[str, str]:
    """Map each manifest's project dir to the rev it pins for ``library``.

    Matches the package name case-insensitively, honouring common aliases
    (``mathlib``/``mathlib4``, ``batteries``/``std4``, …). The dir key is
    workspace-relative (``"."`` for the root).
    """
    wanted = library_package_names(library)
    revs: dict[str, str] = {}
    for manifest, data in _iter_manifests(root):
        for pkg in data.get("packages", []):
            if str(pkg.get("name", "")).lower() in wanted:
                rev = pkg.get("rev") or pkg.get("inputRev")
                if rev:
                    revs[str(manifest.parent.relative_to(root) or ".")] = str(rev)
    return revs
