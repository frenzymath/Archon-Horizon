#!/usr/bin/env python3
"""Synchronize every checked-in Archon Horizon version surface."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-zA-Z0-9.+-]*)$")
SOURCE_PATTERN = re.compile(r'(?m)^__version__ = "([^"]+)"$')
BADGE_PATTERN = re.compile(r"(shields\.io/badge/version-)([^-]+)(-blue)")


def _source_version(root: Path) -> str:
    source = root / "src" / "archon_horizon" / "__init__.py"
    match = SOURCE_PATTERN.search(source.read_text("utf-8"))
    if not match:
        raise ValueError(f"could not find __version__ in {source}")
    return match.group(1)


def _json_versions(root: Path) -> dict[str, str]:
    package_path = root / "src" / "archon_horizon" / "frontend" / "package.json"
    lock_path = package_path.with_name("package-lock.json")
    package = json.loads(package_path.read_text("utf-8"))
    lock = json.loads(lock_path.read_text("utf-8"))
    return {
        "frontend package": str(package.get("version", "")),
        "frontend lock": str(lock.get("version", "")),
        "frontend lock root package": str(lock.get("packages", {}).get("", {}).get("version", "")),
    }


def _read_versions(root: Path) -> dict[str, str]:
    readme = (root / "README.md").read_text("utf-8")
    badge = BADGE_PATTERN.search(readme)
    versions = {
        "Python package": _source_version(root),
        "README badge": badge.group(2) if badge else "<missing>",
        "demo workspace": (root / "demo" / ".archon-horizon" / "version").read_text("utf-8").strip(),
    }
    versions.update(_json_versions(root))
    return versions


def check(root: Path) -> bool:
    expected = _source_version(root)
    drift = {name: value for name, value in _read_versions(root).items() if value != expected}
    if not drift:
        print(f"all version surfaces match {expected}")
        return True
    print(f"version drift (expected {expected} from archon_horizon.__version__):", file=sys.stderr)
    for name, value in drift.items():
        print(f"  {name}: {value}", file=sys.stderr)
    print(f"run: {sys.executable} scripts/version.py {expected}", file=sys.stderr)
    return False


def _replace_once(path: Path, pattern: re.Pattern[str], replacement: str) -> None:
    text = path.read_text("utf-8")
    updated, count = pattern.subn(replacement, text)
    if count != 1:
        raise ValueError(f"expected one version marker in {path}, found {count}")
    path.write_text(updated, "utf-8")


def set_version(root: Path, version: str) -> None:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"invalid version {version!r}; expected MAJOR.MINOR.PATCH")

    _replace_once(
        root / "src" / "archon_horizon" / "__init__.py",
        SOURCE_PATTERN,
        f'__version__ = "{version}"',
    )
    _replace_once(
        root / "README.md",
        BADGE_PATTERN,
        rf"\g<1>{version}\g<3>",
    )

    package_path = root / "src" / "archon_horizon" / "frontend" / "package.json"
    lock_path = package_path.with_name("package-lock.json")
    package = json.loads(package_path.read_text("utf-8"))
    package["version"] = version
    package_path.write_text(json.dumps(package, indent=2) + "\n", "utf-8")
    lock = json.loads(lock_path.read_text("utf-8"))
    lock["version"] = version
    lock.setdefault("packages", {}).setdefault("", {})["version"] = version
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", "utf-8")

    (root / "demo" / ".archon-horizon" / "version").write_text(version + "\n", "utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="new MAJOR.MINOR.PATCH version")
    parser.add_argument("--check", action="store_true", help="fail when checked-in versions drift")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.check:
        if args.version:
            parser.error("--check does not accept a version")
        return 0 if check(root) else 1
    if not args.version:
        parser.error("provide a version or use --check")
    try:
        set_version(root, args.version)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"synchronized Archon Horizon {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
