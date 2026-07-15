from __future__ import annotations

import json

from archon_horizon.core import version


def test_latest_version_message_points_at_horizon_update(monkeypatch) -> None:
    monkeypatch.setattr(version, "latest_available_version", lambda now=None: "0.2.0")

    message = version.latest_version_message(installed="0.1.0")

    assert message is not None
    assert "horizon update" in message
    assert "pipx" not in message


def test_latest_version_message_ignores_current_or_older(monkeypatch) -> None:
    monkeypatch.setattr(version, "latest_available_version", lambda now=None: "0.1.0")
    assert version.latest_version_message(installed="0.1.0") is None

    monkeypatch.setattr(version, "latest_available_version", lambda now=None: "0.0.9")
    assert version.latest_version_message(installed="0.1.0") is None


def test_latest_available_version_uses_fresh_cache(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "latest-version.json"
    cache.write_text(json.dumps({"version": "0.3.0", "fetched_at": 100.0}), "utf-8")
    monkeypatch.setattr(version, "_latest_version_cache_path", lambda: cache)

    def fail_fetch() -> str | None:
        raise AssertionError("fresh cache should avoid network")

    monkeypatch.setattr(version, "_fetch_latest_version", fail_fetch)

    assert version.latest_available_version(now=100.0) == "0.3.0"


def test_latest_available_version_silently_handles_fetch_failure(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "latest-version.json"
    monkeypatch.setattr(version, "_latest_version_cache_path", lambda: cache)
    monkeypatch.setattr(version, "_fetch_latest_version", lambda: None)

    assert version.latest_available_version(now=100.0) is None
    assert json.loads(cache.read_text("utf-8")) == {"version": "", "fetched_at": 100.0}


def test_latest_available_version_uses_fresh_negative_cache(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "latest-version.json"
    cache.write_text(json.dumps({"version": "", "fetched_at": 100.0}), "utf-8")
    monkeypatch.setattr(version, "_latest_version_cache_path", lambda: cache)

    def fail_fetch() -> str | None:
        raise AssertionError("fresh negative cache should avoid network")

    monkeypatch.setattr(version, "_fetch_latest_version", fail_fetch)

    assert version.latest_available_version(now=100.0) is None


def test_warn_if_newer_available_skips_json_and_non_tty(monkeypatch) -> None:
    called = False

    def fake_latest_message() -> str | None:
        nonlocal called
        called = True
        return "newer"

    monkeypatch.setattr(version, "latest_version_message", fake_latest_message)
    monkeypatch.setattr(version.sys.stderr, "isatty", lambda: False)

    version.warn_if_newer_available(json_mode=True)
    version.warn_if_newer_available(json_mode=False)

    assert called is False


def test_parse_package_version() -> None:
    assert version._parse_package_version('__version__ = "1.2.3"\n') == "1.2.3"
    assert version._parse_package_version("x = 1\n") is None
