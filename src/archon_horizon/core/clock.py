"""Single clock seam.

Domain modules import :func:`utc_now` from here instead of reaching into
``events``. A test can monkeypatch this one function to freeze time.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
