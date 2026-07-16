"""Workspace and project contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .types import Metadata


@dataclass(frozen=True, slots=True)
class ProjectVcs:
    """Optional project-local VCS kept outside the project worktree."""

    enabled: bool = False
    git_dir: Path | None = None
    origin: str | None = None
    branch: str | None = None


@dataclass(frozen=True, slots=True)
class Project:
    """A member formalization unit embedded in the workspace."""

    name: str
    path: Path
    type: str = "lean"
    vcs: ProjectVcs = field(default_factory=ProjectVcs)
    blueprint_path: Path | None = None
    build_command: str | None = None
    depends_on: tuple[str, ...] = ()
    # Extra workspace-relative globs associated with this project beyond its own
    # tree (e.g. a shared references/ dir) — advisory scope metadata.
    write_paths: tuple[str, ...] = ()
    metadata: Metadata = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Workspace:
    """The global Archon Horizon root."""

    name: str
    root: Path
    state_dir: Path = Path(".archon-horizon")
    rounds: int = 1
    projects: dict[str, Project] = field(default_factory=dict)
    metadata: Metadata = field(default_factory=dict)

    @property
    def state_path(self) -> Path:
        return self.root / self.state_dir

    def project(self, name: str) -> Project:
        return self.projects[name]

    def project_path(self, name: str) -> Path:
        """Resolve a project's working directory against the workspace root."""
        path = self.projects[name].path
        return path if path.is_absolute() else self.root / path

    def artifact_dir(self, *parts: str) -> Path:
        """A path under ``.archon-horizon/artifacts`` for run/task outputs."""
        return self.state_path.joinpath("artifacts", *parts)
