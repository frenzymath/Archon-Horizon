"""Load ``config.yaml`` and assemble runtime objects.

``build_orchestrator`` is the payoff: a directory containing ``config.yaml``
becomes a wired, runnable :class:`Orchestrator`. Engine selection is entirely
config-driven — the Ground/Horizon agents receive whichever ``Harness`` the
registry built for the names in ``workspace.ground_agent.harness`` /
``horizon_agent.harness``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from archon_horizon.agents.harness_agents import HarnessHorizonAgent, HarnessGroundAgent
from archon_horizon.core.freeze import FreezeLevel, FreezeRule, FreezeSet
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.harnesses.base import Harness
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.log import log
from archon_horizon.orchestration.locks import FilesystemLockManager
from archon_horizon.orchestration.orchestrator import Orchestrator
from archon_horizon.orchestration.scheduler import FreezeAwareScheduler
from archon_horizon.orchestration.sync import MultiProviderSyncCoordinator
from archon_horizon.runlog import RunLogTree
from archon_horizon.subagents.registry import build_subagents
from archon_horizon.store.base import (
    EventLog,
    MemoryStore,
    ReportStore,
    RoadmapStore,
    RunStore,
    TaskStore,
)
from archon_horizon.store.codec import Codec, YamlCodec
from archon_horizon.store.filesystem import (
    FilesystemEventLog,
    FilesystemMemoryStore,
    FilesystemReportStore,
    FilesystemRoadmapStore,
    FilesystemRunStore,
    FilesystemTaskStore,
)

from .harnesses import HarnessRegistry
from .env import load_env_file
from .manifest import find_package_revs
from .schema import WorkspaceConfig

CONFIG_FILENAME = "config.yaml"
_WARNED_LIBRARY_MISMATCHES: set[tuple[str, str, str, str, str]] = set()


def _warn_library_mismatches(cfg: WorkspaceConfig, root: Path) -> None:
    """Warn when a project's ``lake-manifest.json`` pins a rev that differs from
    the rev declared for that library in ``external_libraries``."""
    for lib in cfg.external_libraries:
        if not lib.rev:
            continue
        for project_dir, actual in find_package_revs(root, lib.name).items():
            if actual != lib.rev:
                key = (str(root.resolve()), lib.name, str(project_dir), actual, lib.rev)
                if key in _WARNED_LIBRARY_MISMATCHES:
                    continue
                _WARNED_LIBRARY_MISMATCHES.add(key)
                log.warn(
                    f"Library {lib.name!r} is pinned to {actual!r} in {project_dir}/lake-manifest.json, "
                    f"but external_libraries declares {lib.rev!r}."
                )


def load_config(root: Path) -> WorkspaceConfig:
    load_env_file(root)
    path = root / CONFIG_FILENAME
    if not path.exists():
        raise FileNotFoundError(
            f"No {CONFIG_FILENAME} found at {root.resolve()} — this is not an Archon Horizon "
            "workspace. Run `horizon init` here first (use `--root <dir>` to target another directory)."
        )
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    cfg = WorkspaceConfig.from_raw(raw)
    _warn_library_mismatches(cfg, root)
    return cfg


def build_workspace(cfg: WorkspaceConfig, root: Path) -> Workspace:
    projects = {
        name: Project(
            name=pc.name,
            path=Path(pc.path),
            type=pc.type,
            vcs=pc.vcs,
            blueprint_path=Path(pc.blueprint_path) if pc.blueprint_path else None,
            build_command=pc.build_command,
            depends_on=pc.depends_on,
            write_paths=pc.write_paths,
        )
        for name, pc in cfg.projects.items()
    }
    return Workspace(
        name=cfg.name,
        root=root,
        state_dir=Path(cfg.state_dir),
        rounds=cfg.rounds,
        projects=projects,
    )


def build_freeze(cfg: WorkspaceConfig) -> FreezeSet:
    rules: list[FreezeRule] = []
    # Workspace-level freeze (top-level `freeze:` section).
    rules.extend(FreezeRule(level=FreezeLevel.AGENT, pattern=a) for a in cfg.freeze_agents)
    rules.extend(FreezeRule(level=FreezeLevel.PROJECT, pattern=p) for p in cfg.freeze_projects)
    rules.extend(FreezeRule(level=FreezeLevel.FILE, pattern=f) for f in cfg.freeze_files)
    rules.extend(FreezeRule(level=FreezeLevel.DECLARATION, pattern=d) for d in cfg.freeze_declarations)
    rules.extend(FreezeRule(level=FreezeLevel.BLUEPRINT_NODE, pattern=n) for n in cfg.freeze_blueprint_nodes)
    # Per-project freeze.
    for pc in cfg.projects.values():
        rules.extend(FreezeRule(level=FreezeLevel.FILE, pattern=f) for f in pc.freeze_files)
        rules.extend(
            FreezeRule(level=FreezeLevel.DECLARATION, pattern=d) for d in pc.freeze_declarations
        )
    return FreezeSet(tuple(rules))


@dataclass(frozen=True, slots=True)
class Stores:
    events: EventLog
    roadmap: RoadmapStore
    memory: MemoryStore
    tasks: TaskStore
    runs: RunStore
    reports: ReportStore
    run_logs: RunLogTree


def build_stores(workspace: Workspace, codec: Codec | None = None) -> Stores:
    codec = codec or YamlCodec()
    state = workspace.state_path
    run_logs = RunLogTree(state / "runs")
    return Stores(
        events=FilesystemEventLog(state / "events.jsonl"),
        roadmap=FilesystemRoadmapStore(state / "roadmap", codec),
        memory=FilesystemMemoryStore(state / "memory.md"),
        tasks=FilesystemTaskStore(state / "tasks", codec),
        runs=FilesystemRunStore(run_logs, codec),
        reports=FilesystemReportStore(state / "reports", state),
        run_logs=run_logs,
    )


def _resolve_harness(
    harnesses: dict[str, Harness], name: str | None, role: str
) -> Harness:
    if name is None:
        raise ValueError(f"config does not set a harness for the {role} agent")
    try:
        return harnesses[name]
    except KeyError as exc:
        raise ValueError(
            f"{role} agent references harness {name!r}, which is not defined "
            f"under 'harnesses' (defined: {sorted(harnesses)})"
        ) from exc


def build_orchestrator(
    root: Path,
    *,
    registry: HarnessRegistry | None = None,
    harnesses: dict[str, Harness] | None = None,
    inbox_providers: Sequence[InboxProvider] = (),
    codec: Codec | None = None,
) -> Orchestrator:
    """Wire a runnable orchestrator from ``<root>/config.yaml``.

    Pass ``harnesses`` to override the built engines (e.g. a NullHarness map
    in tests); otherwise the ``registry`` builds them from config.
    """
    cfg = load_config(root)
    workspace = build_workspace(cfg, root)
    freeze = build_freeze(cfg)

    built = harnesses if harnesses is not None else (registry or HarnessRegistry()).build_all(cfg.harnesses)
    ground_harness = _resolve_harness(built, cfg.ground_harness, "ground")
    ground = HarnessGroundAgent(ground_harness)
    horizon = HarnessHorizonAgent(_resolve_harness(built, cfg.horizon_harness, "horizon"))
    subagent_harness = (
        _resolve_harness(built, cfg.subagent_harness, "subagent")
        if cfg.subagent_harness
        else ground_harness
    )
    ground_subagents = build_subagents(
        cfg.ground_subagents,
        descriptor_dir=workspace.state_path / "subagents",
        harnesses=built,
        default_harness=subagent_harness,
    )

    stores = build_stores(workspace, codec)
    return Orchestrator(
        workspace=workspace,
        ground=ground,
        horizon=horizon,
        scheduler=FreezeAwareScheduler(
            freeze=freeze, max_parallel=cfg.scheduler.max_parallel_sessions
        ),
        sync=MultiProviderSyncCoordinator(inbox_providers),
        locks=FilesystemLockManager(workspace.state_path / "locks"),
        event_log=stores.events,
        roadmap_store=stores.roadmap,
        memory_store=stores.memory,
        task_store=stores.tasks,
        inbox_providers=inbox_providers,
        run_store=stores.runs,
        run_logs=stores.run_logs,
        ground_subagents=ground_subagents,
        freeze=freeze,
        start_with=cfg.start_with,
        end_with=cfg.end_with,
    )
