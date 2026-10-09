#!/usr/bin/env python3
"""Copy the standalone demo into an already built documentation site."""

import argparse
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, default=ROOT / "_site")
    parser.add_argument("--demo", type=Path, default=ROOT / "src/archon_horizon/frontend/build/dashboard-demo")
    args = parser.parse_args()
    if not (args.site / "index.html").is_file() or not (args.demo / "index.html").is_file():
        parser.error("Build both MkDocs and the standalone dashboard demo first.")
    destination = args.site / "demo"
    if destination.is_symlink():
        parser.error("Refusing a symlinked demo destination.")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(args.demo, destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
