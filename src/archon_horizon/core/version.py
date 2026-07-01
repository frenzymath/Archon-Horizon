"""Workspace ↔ tool version stamping and drift detection.

``horizon init`` (and ``horizon init --update``) stamps the running Horizon
version into the workspace state directory as ``.archon-horizon/version``.
Workspace-touching commands then compare that stamp against the installed
:data:`archon_horizon.__version__` and warn on drift, pointing the user at the
right remedy:

* installed **newer** than the stamp → the *workspace* is stale; run
  ``horizon init --update`` to refresh managed files (skills, subagents, MCP);
* installed **older** than the stamp → the *tool* is stale; run
  ``horizon update`` to upgrade Horizon.

The stamp lives inside the tracked state dir, so a cloned/shared workspace
records which Horizon built it. The check is best-effort and never fatal: a
missing or unparseable stamp degrades to a gentle nudge, never an error.
"""

from __future__ import annotations

import re
from pathlib import Path

from archon_horizon import __version__

VERSION_FILENAME = "version"
_DEFAULT_STATE_DIR = Path(".archon-horizon")

# Module-level latch so a single CLI invocation warns at most once even when
# several commands load the workspace.
_warned_roots: set[str] = set()


def _version_path(root: Path, state_dir: Path = _DEFAULT_STATE_DIR) -> Path:
    return root / state_dir / VERSION_FILENAME


def read_workspace_version(root: Path, state_dir: Path = _DEFAULT_STATE_DIR) -> str | None:
    """Return the Horizon version stamped into the workspace, or ``None``.

    ``None`` means either the workspace is not initialized or it predates
    version stamping.
    """
    path = _version_path(root, state_dir)
    try:
        text = path.read_text("utf-8").strip()
    except (OSError, ValueError):
        return None
    return text or None


def stamp_workspace_version(
    root: Path,
    state_dir: Path = _DEFAULT_STATE_DIR,
    version: str = __version__,
) -> None:
    """Record ``version`` (default: the installed Horizon) in the state dir."""
    path = _version_path(root, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(version.strip() + "\n", "utf-8")


def _release_key(version: str) -> tuple[int, ...]:
    """A comparable tuple of the leading numeric ``MAJOR.MINOR.PATCH`` fields.

    Pre-release/local suffixes (``-rc1``, ``+local``) are ignored for ordering;
    this only decides which *remedy* to suggest, so coarse comparison is fine.
    Unparseable input yields ``()`` which compares as the lowest version.
    """
    head = re.split(r"[-+]", version.strip(), maxsplit=1)[0]
    parts = head.split(".")
    out: list[int] = []
    for part in parts:
        if part.isdigit():
            out.append(int(part))
        else:
            break
    return tuple(out)


def version_drift_message(
    root: Path,
    state_dir: Path = _DEFAULT_STATE_DIR,
    installed: str = __version__,
) -> str | None:
    """Return a one-line drift warning for this workspace, or ``None``.

    ``None`` when the workspace is not initialized, the versions match, or the
    comparison is inconclusive.
    """
    if not (root / state_dir).is_dir():
        return None
    stamped = read_workspace_version(root, state_dir)
    if stamped is None:
        return (
            "This workspace predates Horizon version tracking. Run "
            "`horizon init --update` to refresh managed files and record the version."
        )
    if stamped == installed:
        return None

    installed_key, stamped_key = _release_key(installed), _release_key(stamped)
    if installed_key > stamped_key:
        return (
            f"This workspace was initialized with Horizon {stamped}, but you are running "
            f"{installed}. Run `horizon init --update` to refresh managed files "
            "(skills, subagents, MCP) for the newer Horizon."
        )
    if installed_key < stamped_key:
        return (
            f"This workspace expects Horizon {stamped}, but you are running the older "
            f"{installed}. Run `horizon update` to upgrade Horizon (then `horizon init --update`)."
        )
    # Equal numeric release, differing suffixes — not worth nagging about.
    return None


def warn_on_drift(root: Path, state_dir: Path = _DEFAULT_STATE_DIR) -> None:
    """Emit the drift warning at most once per process for this workspace."""
    key = str(root.resolve())
    if key in _warned_roots:
        return
    message = version_drift_message(root, state_dir)
    if message is None:
        return
    _warned_roots.add(key)
    from archon_horizon.log import log

    log.warn(message)
