#!/usr/bin/env python3
"""Check that source releases can rebuild Horizon without local operator files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import sys
import tarfile


REQUIRED = {
    "docs/pipeline-milestones.md",
    "tests/test_pipeline_milestones.py",
    "tests/test_pipeline_milestone_api.py",
    "src/archon_horizon/pipeline/skills/operations/horizon-graph/references/milestones.md",
    "pyproject.toml", "MANIFEST.in", "README.md", "CONTRIBUTING.md",
    "AGENTS.md", "CLAUDE.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md",
    "install.sh", "uv.lock", ".githooks/commit-msg", "docs/README.md", "docs/architecture.md",
    "scripts/version.py", "scripts/check_wheel.py", "scripts/check_sdist.py",
    "tests/test_package_imports.py",
    "docs/pipeline-setup.md", "deploy/pipeline/postgres.compose.yaml", "deploy/pipeline/project.example.json",
    "src/archon_horizon/pipeline/skills/operations/horizon-pipeline/SKILL.md",
    "src/archon_horizon/pipeline/skills/lean/lean-performance/scripts/compare_measurements.py",
    "src/archon_horizon/pipeline/skills/review/library-audit/SKILL.md",
    "src/archon_horizon/pipeline/subagents/reviewers/library-api.md",
    "src/archon_horizon/pipeline/subagents/implementation/lean-worker.md",
    "src/archon_horizon/pipeline/subagents/research/page-transcriber.md",
    "src/archon_horizon/pipeline/subagents/validation/build-checker.md",
    "src/archon_horizon/pipeline/subagents/planning/graph-planner.md",
    "src/archon_horizon/__init__.py",
    "src/archon_horizon/frontend/index.html",
    "src/archon_horizon/frontend/package.json",
    "src/archon_horizon/frontend/package-lock.json",
    "src/archon_horizon/frontend/tsconfig.json",
    "src/archon_horizon/frontend/vite.config.ts",
    "src/archon_horizon/frontend/src/main.tsx",
    "src/archon_horizon/frontend/tests/pipeline.browser.cjs",
    "src/archon_horizon/frontend/dist/index.html",
}
LOCAL_DIRECTORIES = {
    ".git", ".claude", ".codex", ".archon-horizon", ".horizon",
    ".horizon-agent-library", ".venv", ".venv-horizon", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", "__pycache__",
    "agent-library", "lean-cache", "ignored-folder", "demo",
}


def check_archive(archive: Path, root: Path) -> list[str]:
    failures = []
    names = set()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                failures.append(f"invalid archive path: {member.name}")
                continue
            if len(path.parts) < 2:
                continue
            relative = PurePosixPath(*path.parts[1:])
            name = relative.as_posix()
            if (LOCAL_DIRECTORIES.intersection(relative.parts)
                    or relative.parts[0] in {"build", "dist"}
                    or name == "config.yaml"
                    or relative.name in {".env", ".mcp.json"}
                    or relative.name.startswith(".env.")
                    or relative.suffix in {".pyc", ".pyo", ".token", ".sqlite"}
                    or ".sqlite-" in relative.name):
                failures.append(f"local-only file: {name}")
            if not member.isfile():
                if not member.isdir():
                    failures.append(f"unsupported archive entry: {name}")
                continue
            names.add(name)
            local = root / name
            # Setuptools adds metadata and an egg_info-only setup.cfg to sdists.
            generated = (name == "PKG-INFO" or (name == "setup.cfg" and not local.exists())
                         or any(part.endswith(".egg-info") for part in relative.parts))
            if not generated:
                content = source.extractfile(member)
                if not local.is_file() or content is None or content.read() != local.read_bytes():
                    failures.append(f"stale or unexpected source: {name}")
    failures.extend(f"missing: {name}" for name in sorted(REQUIRED - names))
    return failures


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    archives = sorted((root / "dist").glob("*.tar.gz"))
    if not archives:
        print("error: no source archive under dist/; run `python -m build` first", file=sys.stderr)
        return 1
    failed = False
    for archive in archives:
        failures = check_archive(archive, root)
        print(f"checked {archive.name}")
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        failed |= bool(failures)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
