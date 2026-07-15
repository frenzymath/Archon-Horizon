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

import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

from archon_horizon import __version__

VERSION_FILENAME = "version"
_DEFAULT_STATE_DIR = Path(".archon-horizon")
LATEST_VERSION_URL = "https://raw.githubusercontent.com/frenzymath/Archon-Horizon/refs/heads/main/src/archon_horizon/__init__.py"
LATEST_VERSION_CACHE_TTL_SECONDS = 24 * 60 * 60
LATEST_VERSION_TIMEOUT_SECONDS = 0.75

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


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "archon-horizon"
    return Path.home() / ".cache" / "archon-horizon"


def _latest_version_cache_path() -> Path:
    return _cache_dir() / "latest-version.json"


def _read_latest_version_cache(now: float) -> tuple[bool, str | None]:
    try:
        data = json.loads(_latest_version_cache_path().read_text("utf-8"))
        fetched_at = float(data.get("fetched_at", 0))
        version = str(data.get("version", "")).strip()
    except Exception:
        return False, None
    if now - fetched_at > LATEST_VERSION_CACHE_TTL_SECONDS:
        return False, None
    return True, version or None


def _write_latest_version_cache(version: str | None, now: float) -> None:
    try:
        path = _latest_version_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": version or "", "fetched_at": now}) + "\n", "utf-8")
    except Exception:
        return


def _parse_package_version(source: str) -> str | None:
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    if not match:
        return None
    version = match.group(1).strip()
    return version or None


def _fetch_latest_version() -> str | None:
    try:
        request = Request(LATEST_VERSION_URL, headers={"User-Agent": f"archon-horizon/{__version__}"})
        with urlopen(request, timeout=LATEST_VERSION_TIMEOUT_SECONDS) as response:
            source = response.read(64_000).decode("utf-8", errors="replace")
    except Exception:
        return None
    return _parse_package_version(source)


def latest_available_version(now: float | None = None) -> str | None:
    """Return the latest upstream Horizon version, or ``None``.

    This is deliberately best-effort: stale/missing cache, network failures,
    malformed upstream content, and unwritable cache dirs all degrade silently.
    """
    clock = time.time() if now is None else now
    cache_fresh, cached = _read_latest_version_cache(clock)
    if cache_fresh:
        return cached
    latest = _fetch_latest_version()
    _write_latest_version_cache(latest, clock)
    return latest


def _should_check_latest_version(json_mode: bool) -> bool:
    if json_mode:
        return False
    if os.environ.get("ARCHON_HORIZON_NO_VERSION_CHECK"):
        return False
    if os.environ.get("ARCHON_HORIZON_AGENT_ROLE"):
        return False
    if os.environ.get("CI"):
        return False
    try:
        return sys.stderr.isatty()
    except Exception:
        return False


def latest_version_message(installed: str = __version__, now: float | None = None) -> str | None:
    """Return an update advisory when upstream is newer, otherwise ``None``."""
    latest = latest_available_version(now)
    if latest is None:
        return None
    if _release_key(latest) <= _release_key(installed):
        return None
    return (
        f"Archon Horizon {latest} is available; you have {installed}. "
        "Run `horizon update` to upgrade, then `horizon init --update` in existing workspaces."
    )


def warn_if_newer_available(*, json_mode: bool = False) -> None:
    """Best-effort upstream version check.

    The check must never affect command behavior. It is silent for machine
    output, CI, agent subcommands, non-TTY runs, disabled envs, and every
    network/cache/parsing failure.
    """
    if not _should_check_latest_version(json_mode):
        return
    try:
        message = latest_version_message()
    except Exception:
        return
    if not message:
        return
    from archon_horizon.log import log

    log.warn_stderr(message)


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
