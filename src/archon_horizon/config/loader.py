"""Load ``config.yaml`` and assemble runtime objects.

``build_orchestrator`` is the payoff: a directory containing ``config.yaml``
becomes a wired, runnable :class:`Orchestrator`. Engine selection is entirely
config-driven — the informal/horizon agents receive whichever ``Harness`` the
registry built for the names in ``workspace.informal_agent.harness`` /
``horizon_agent.harness``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from archon_horizon.agents.harness_agents import HarnessHorizonAgent, HarnessInformalAgent
from archon_horizon.core.freeze import FreezeLevel, FreezeRule, FreezeSet
from archon_horizon.core.workspace import Project, Workspace
from archon_horizon.harnesses.base import Harness
from archon_horizon.inboxes.base import InboxProvider
from archon_horizon.orchestration.locks import FilesystemLockManager
from archon_horizon.orchestration.orchestrator import Orchestrator
from archon_horizon.orchestration.scheduler import FreezeAwareScheduler
from archon_horizon.orchestration.sync import MultiProviderSyncCoordinator
from archon_horizon.runlog import RunLogTree
from archon_horizon.subagents.registry import build_subagents
from archon_horizon.store.base import (
    EventLog,
    MemoryStore,
    ProposalStore,
    ReportStore,
    RoadmapStore,
    RunStore,
    TaskStore,
)
from archon_horizon.store.codec import Codec, YamlCodec
from archon_horizon.store.filesystem import (
    FilesystemEventLog,
    FilesystemMemoryStore,
    FilesystemProposalStore,
    FilesystemReportStore,
    FilesystemRoadmapStore,
    FilesystemRunStore,
    FilesystemTaskStore,
)

from .harnesses import HarnessRegistry
from .schema import WorkspaceConfig

CONFIG_FILENAME = "config.yaml"


def load_config(root: Path) -> WorkspaceConfig:
    path = root / CONFIG_FILENAME
    raw = yaml.safe_load(path.read_text("utf-8")) or {}
    return WorkspaceConfig.from_raw(raw)


def build_workspace(cfg: WorkspaceConfig, root: Path) -> Workspace:
    projects = {
        name: Project(
            name=pc.name,
            path=Path(pc.path),
            type=pc.type,
            vcs=pc.vcs,
            blueprint_path=Path(pc.blueprint_path) if pc.blueprint_path else None,
            build_command=pc.build_command,
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
    proposals: ProposalStore
    runs: RunStore
    reports: ReportStore
    run_logs: RunLogTree


def build_stores(workspace: Workspace, codec: Codec | None = None) -> Stores:
    codec = codec or YamlCodec()
    state = workspace.state_path
    run_logs = RunLogTree(state / "runs")
    return Stores(
        events=FilesystemEventLog(state / "events.jsonl"),
        roadmap=FilesystemRoadmapStore(state / f"roadmap.{codec.extension}", codec),
        memory=FilesystemMemoryStore(state / "memory.md"),
        tasks=FilesystemTaskStore(state / "tasks", codec),
        proposals=FilesystemProposalStore(state / "proposals", codec),
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
    informal_harness = _resolve_harness(built, cfg.informal_harness, "informal")
    informal = HarnessInformalAgent(informal_harness)
    horizon = HarnessHorizonAgent(_resolve_harness(built, cfg.horizon_harness, "horizon"))
    informal_subagents = build_subagents(
        cfg.informal_subagents,
        descriptor_dir=workspace.state_path / "subagents",
        harnesses=built,
        default_harness=informal_harness,
    )

    stores = build_stores(workspace, codec)
    return Orchestrator(
        workspace=workspace,
        informal=informal,
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
        proposal_store=stores.proposals,
        inbox_providers=inbox_providers,
        report_store=stores.reports,
        run_store=stores.runs,
        run_logs=stores.run_logs,
        informal_subagents=informal_subagents,
        freeze=freeze,
    )
