"""Parsed ``config.yaml`` — stable workspace/project settings only.

Mirrors the roadmap's config shape. This is the raw, validated config; the
loader turns it into the runtime objects (``Workspace``, ``FreezeSet``,
``Harness`` instances). Runtime state (tasks, proposals, runs) is NOT here —
it lives under ``.archon-horizon/`` and is owned by the stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import ProjectVcs


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    name: str
    kind: str
    command: str | None = None
    args: tuple[str, ...] = ()
    model: str | None = None
    options: Metadata = field(default_factory=dict)

    @classmethod
    def from_raw(cls, name: str, data: dict[str, Any]) -> "HarnessConfig":
        return cls(
            name=name,
            kind=data["kind"],
            command=data.get("command"),
            args=tuple(data.get("args", ())),
            model=data.get("model"),
            options=dict(data.get("options", {})),
        )


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    max_parallel_sessions: int = 1
    unknown_write_set_policy: str = "lock-project"

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "SchedulerConfig":
        return cls(
            max_parallel_sessions=int(data.get("max_parallel_sessions", 1)),
            unknown_write_set_policy=data.get("unknown_write_set_policy", "lock-project"),
        )


@dataclass(frozen=True, slots=True)
class GithubConfig:
    enabled: bool = False
    repo: str | None = None
    import_policy: str = "labeled-only"
    labels: Metadata = field(default_factory=dict)

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "GithubConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            repo=data.get("repo"),
            import_policy=data.get("import_policy", "labeled-only"),
            labels=dict(data.get("labels", {})),
        )


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    name: str
    path: str
    type: str = "lean"
    vcs: ProjectVcs = field(default_factory=ProjectVcs)
    blueprint_path: str | None = None
    build_command: str | None = None
    freeze_files: tuple[str, ...] = ()
    freeze_declarations: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, name: str, data: dict[str, Any]) -> "ProjectConfig":
        vcs_raw = data.get("vcs", {})
        git_dir = vcs_raw.get("git_dir")
        freeze_raw = data.get("freeze", {})
        return cls(
            name=name,
            path=data["path"],
            type=data.get("type", "lean"),
            vcs=ProjectVcs(
                enabled=bool(vcs_raw.get("enabled", True)),
                git_dir=Path(git_dir) if git_dir else None,
                origin=vcs_raw.get("origin"),
                branch=vcs_raw.get("branch"),
            ),
            blueprint_path=data.get("blueprint", {}).get("path"),
            build_command=data.get("build", {}).get("command"),
            freeze_files=tuple(freeze_raw.get("files", ())),
            freeze_declarations=tuple(freeze_raw.get("declarations", ())),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    name: str
    state_dir: str = ".archon-horizon"
    rounds: int = 1
    informal_harness: str | None = None
    horizon_harness: str | None = None
    informal_subagents: tuple[str, ...] | None = None
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    harnesses: dict[str, HarnessConfig] = field(default_factory=dict)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    github: GithubConfig = field(default_factory=GithubConfig)
    freeze_agents: tuple[str, ...] = ()
    freeze_projects: tuple[str, ...] = ()
    freeze_files: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "WorkspaceConfig":
        ws = data.get("workspace", {})
        subagents = ws.get("informal_agent", {}).get("subagents")
        freeze = data.get("freeze", {})
        return cls(
            name=ws["name"],
            state_dir=ws.get("state_dir", ".archon-horizon"),
            rounds=int(ws.get("rounds", 1)),
            informal_harness=ws.get("informal_agent", {}).get("harness"),
            horizon_harness=ws.get("horizon_agent", {}).get("harness"),
            informal_subagents=tuple(subagents) if subagents is not None else None,
            scheduler=SchedulerConfig.from_raw(ws.get("scheduler", {})),
            harnesses={
                name: HarnessConfig.from_raw(name, h)
                for name, h in data.get("harnesses", {}).items()
            },
            projects={
                name: ProjectConfig.from_raw(name, p)
                for name, p in data.get("projects", {}).items()
            },
            github=GithubConfig.from_raw(data.get("github", {})),
            freeze_agents=tuple(freeze.get("agents", ())),
            freeze_projects=tuple(freeze.get("projects", ())),
            freeze_files=tuple(freeze.get("files", ())),
        )
