"""Write-permission domains — the allowlist complement to freeze.

Freeze (:mod:`archon_horizon.core.freeze`) is a denylist of protected targets.
A :class:`WriteDomain` is the positive lane an agent is expected to stay in:
which workspace-relative paths it may edit. The Horizon agent runs free inside
its lane; the Ground agent has a wider one. Reads are unrestricted
(cross-project reads are allowed by design), so this models writes only.

Pure: glob matching only, no I/O. Enforcement (post-hoc diff checks) lives in
the orchestrator; this module just decides coverage and builds default lanes.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from .workspace import Workspace


def glob_covers(pattern: str, path: str) -> bool:
    """Whether ``pattern`` covers workspace-relative ``path``.

    ``dir/**`` covers ``dir`` and everything beneath it; ``*.lean`` and other
    fnmatch patterns work too; a bare path matches exactly.
    """
    pattern = pattern.strip("/").replace("\\", "/")
    path = path.strip("/").replace("\\", "/")
    if pattern in ("**", "*"):
        return True
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return path == pattern or fnmatch.fnmatch(path, pattern)


@dataclass(frozen=True, slots=True)
class WriteDomain:
    """An allowlist of workspace-relative globs an agent may write."""

    allow: tuple[str, ...] = ()

    def covers(self, rel_path: str) -> bool:
        return any(glob_covers(g, rel_path) for g in self.allow)

    def violations(self, rel_paths: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(p for p in rel_paths if not self.covers(p))


def _project_globs(workspace: Workspace, name: str, seen: set[str] | None = None) -> list[str]:
    seen = seen or set()
    if name in seen:
        return []
    seen.add(name)
    proj = workspace.project(name)
    globs = [f"{Path(proj.path).as_posix()}/**"]
    if proj.blueprint_path is not None:
        globs.append(f"{Path(proj.blueprint_path).as_posix()}/**")
    globs.extend(proj.write_paths)
    for dep in proj.depends_on:
        if dep in workspace.projects:
            globs.extend(_project_globs(workspace, dep, seen))
    return globs


def _dedup(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def horizon_write_domain(workspace: Workspace, projects: tuple[str, ...]) -> WriteDomain:
    """The free Horizon agent's lane: its task's project trees (+ blueprints) and
    the shared references library.

    A task may span several projects (workspace-level freedom); pass them all.
    Memory and inbox are mutated through the CLI, not by writing files, so they
    are not part of the file write-lane.
    """
    allow: list[str] = []
    for name in projects:
        allow.extend(_project_globs(workspace, name))
    allow.append("references/**")
    return WriteDomain(_dedup(allow))


def ground_write_domain(workspace: Workspace, projects: tuple[str, ...]) -> WriteDomain:
    """The janitor's wider lane: the scope's project trees + shared state docs."""
    state = workspace.state_dir.as_posix()
    allow: list[str] = []
    for name in projects:
        allow.extend(_project_globs(workspace, name))
    allow += [f"{state}/roadmap/**", f"{state}/blueprints/**"]
    return WriteDomain(_dedup(allow))
