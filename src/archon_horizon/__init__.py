"""Archon Horizon core package."""

__all__ = ["__version__"]

# Single source of truth for the package version. ``pyproject.toml`` reads this
# attribute via setuptools' dynamic version. ``scripts/version.py`` treats this
# value as authoritative and synchronizes the other checked-in surfaces.
__version__ = "0.1.1"
