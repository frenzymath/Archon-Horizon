"""Archon Horizon core package."""

__all__ = ["__version__"]

# Single source of truth for the package version. ``pyproject.toml`` reads this
# attribute via setuptools' dynamic version, so this string is the only place a
# release bump happens. Keep it PEP 440 compatible (``MAJOR.MINOR.PATCH``).
__version__ = "0.1.0"

