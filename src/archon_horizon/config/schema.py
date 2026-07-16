"""Parsed ``config.yaml`` — stable workspace/project settings only.

Mirrors the roadmap's config shape. This is the raw, validated config; the
loader turns it into the runtime objects (``Workspace``, ``FreezeSet``,
``Harness`` instances). Runtime state (tasks, runs) is NOT here —
it lives under ``.archon-horizon/`` and is owned by the stores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archon_horizon.core.types import Metadata
from archon_horizon.core.workspace import ProjectVcs


# Symbolic model tiers, ordered cheapest → most capable. A subagent descriptor
# names a tier (engine-agnostic); the harness's ``models`` map turns it into a
# concrete model. ``big`` defaults to the harness's primary ``model``.
TIER_ORDER: tuple[str, ...] = ("small", "medium", "big")


@dataclass(frozen=True, slots=True)
class HarnessConfig:
    name: str
    kind: str
    command: str | None = None
    args: tuple[str, ...] = ()
    model: str | None = None
    # tier name → concrete model (e.g. {"small": "haiku", "medium": "sonnet"}).
    # Must stay within this harness's own provider — native subagents inherit the
    # parent process env, so a cross-provider tier would not route correctly.
    models: dict[str, str] = field(default_factory=dict)
    options: Metadata = field(default_factory=dict)

    @classmethod
    def from_raw(cls, name: str, data: dict[str, Any]) -> "HarnessConfig":
        raw_models = data.get("models") or {}
        return cls(
            name=name,
            kind=data["kind"],
            command=data.get("command"),
            args=tuple(data.get("args", ())),
            model=data.get("model"),
            models={str(k): str(v) for k, v in raw_models.items()},
            options=dict(data.get("options", {})),
        )

    def tier_model(self, tier: str | None) -> str | None:
        """Resolve a tier name to a concrete model for this harness.

        ``model`` is the primary/``big`` default. A requested tier that isn't
        filled in falls *up* toward the next-more-capable tier, then to ``model``.
        Returns ``None`` when nothing is configured, so callers omit the model and
        let the subagent inherit the parent session's model.
        """
        if not tier:
            return self.model
        if tier not in TIER_ORDER:
            return self.models.get(tier) or self.model
        for t in TIER_ORDER[TIER_ORDER.index(tier):]:
            if t == "big":
                return self.models.get("big") or self.model
            if self.models.get(t):
                return self.models[t]
        return self.model

    @property
    def config_dir(self) -> str | None:
        """The per-harness config/home directory override, if any.

        One option (`config_dir`) maps to whichever directory the engine uses for
        its config home — auth + sessions + user-level config (`CLAUDE_CONFIG_DIR`
        / `CODEX_HOME`). Pins each harness to its account dir. Workspace-local
        behavior (agents, skills, project MCP) is written separately, under the
        workspace, and is not affected by this.
        """
        raw = self.options.get("config_dir")
        value = str(raw).strip() if raw else ""
        return value or None


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    max_parallel_sessions: int = 1

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "SchedulerConfig":
        return cls(
            max_parallel_sessions=int(data.get("max_parallel_sessions", 1)),
        )


@dataclass(frozen=True, slots=True)
class GithubConfig:
    enabled: bool = False
    repo: str | None = None
    # Import ALL issues/PRs by default so a freshly-opened, unlabelled issue is
    # visible in the dashboard for a human to triage/label — otherwise
    # "labeled-only" hides exactly the items that still need a label
    # (chicken-and-egg). Set import_policy explicitly to restrict.
    import_policy: str = "all"

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "GithubConfig":
        return cls(
            enabled=bool(data.get("enabled", False)),
            repo=data.get("repo"),
            import_policy=data.get("import_policy", "all"),
        )


# Bare library names we know how to clone without an explicit url, so a config
# can say just ``name: mathlib`` and get the canonical GitHub repo. Anything not
# listed must give a ``github:`` shorthand or a full ``git:`` url.
KNOWN_LIBRARY_GIT: dict[str, str] = {
    "mathlib": "https://github.com/leanprover-community/mathlib4.git",
    "mathlib4": "https://github.com/leanprover-community/mathlib4.git",
    "batteries": "https://github.com/leanprover-community/batteries.git",
    "std4": "https://github.com/leanprover-community/batteries.git",
    "aesop": "https://github.com/leanprover-community/aesop.git",
    "proofwidgets": "https://github.com/leanprover-community/ProofWidgets4.git",
    "plausible": "https://github.com/leanprover-community/plausible.git",
    "qq": "https://github.com/leanprover-community/quote4.git",
    "importgraph": "https://github.com/leanprover-community/import-graph.git",
    "leansearchclient": "https://github.com/leanprover-community/LeanSearchClient.git",
}


@dataclass(frozen=True, slots=True)
class ExternalLibrary:
    """One Lean dependency the agents and the search index should know about.

    A library resolves to a git repo by precedence: explicit ``git`` url, then a
    ``github`` ``owner/repo`` shorthand, then an ``owner/repo`` embedded in the
    name, then the known-library table (so ``name: mathlib`` just works). ``rev``
    pins a tag/branch/commit; ``path`` points the indexer at already-checked-out
    sources (skipping any ``.lake`` lookup) for vendored or non-lake libraries.
    """

    name: str
    rev: str | None = None
    git: str | None = None
    github: str | None = None
    path: str | None = None

    @classmethod
    def from_raw(cls, data: Any) -> "ExternalLibrary":
        # String shorthand: "name", "owner/repo", or either with "@rev".
        if isinstance(data, str):
            spec = data.strip()
            rev: str | None = None
            if "@" in spec and not spec.startswith("git@"):
                spec, _, rev = spec.rpartition("@")
            return cls(name=spec.strip(), rev=rev or None)
        if not isinstance(data, dict):
            raise ValueError(f"external library entry must be a string or mapping, got {data!r}")
        name = data.get("name")
        if not name:
            raise ValueError("external library entry needs a 'name'")
        return cls(
            name=str(name),
            rev=str(data["rev"]) if data.get("rev") is not None else None,
            git=data.get("git"),
            github=data.get("github"),
            path=data.get("path"),
        )

    @property
    def git_url(self) -> str | None:
        """Resolve a clone url, defaulting to GitHub. ``None`` if unresolvable."""
        if self.git:
            return self.git
        if self.github:
            owner_repo = self.github.removesuffix(".git")
            return f"https://github.com/{owner_repo}.git"
        # A bare "owner/repo" name is treated as a GitHub repo by default.
        if "/" in self.name and not self.name.startswith(("http", "git@", "ssh:")):
            return f"https://github.com/{self.name.removesuffix('.git')}.git"
        return KNOWN_LIBRARY_GIT.get(self.name.lower())


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    name: str
    path: str
    type: str = "lean"
    vcs: ProjectVcs = field(default_factory=ProjectVcs)
    blueprint_path: str | None = None
    build_command: str | None = None
    depends_on: tuple[str, ...] = ()
    freeze_files: tuple[str, ...] = ()
    freeze_declarations: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()

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
            depends_on=tuple(data.get("depends_on", ())),
            freeze_files=tuple(freeze_raw.get("files", ())),
            freeze_declarations=tuple(freeze_raw.get("declarations", ())),
            write_paths=tuple(data.get("write_paths", ())),
        )


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    name: str
    state_dir: str = ".archon-horizon"
    rounds: int = 1
    # Harness for auxiliary human-driven sessions (`horizon discuss`, the
    # post-init advisor) and the default for `horizon subagent`. The orchestrated
    # run loop is horizon-only and never reads this.
    ground_harness: str | None = None
    horizon_harness: str | None = None
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    external_libraries: tuple[ExternalLibrary, ...] = ()
    harnesses: dict[str, HarnessConfig] = field(default_factory=dict)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    github: GithubConfig = field(default_factory=GithubConfig)
    freeze_agents: tuple[str, ...] = ()
    freeze_projects: tuple[str, ...] = ()
    freeze_files: tuple[str, ...] = ()
    freeze_declarations: tuple[str, ...] = ()
    freeze_blueprint_nodes: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "WorkspaceConfig":
        ws = data.get("workspace", {})
        freeze = data.get("freeze", {})
        return cls(
            name=ws["name"],
            state_dir=ws.get("state_dir", ".archon-horizon"),
            rounds=int(ws.get("rounds", 1)),
            ground_harness=ws.get("ground_agent", {}).get("harness"),
            horizon_harness=ws.get("horizon_agent", {}).get("harness"),
            scheduler=SchedulerConfig.from_raw(ws.get("scheduler", {})),
            external_libraries=tuple(
                ExternalLibrary.from_raw(e) for e in (data.get("external_libraries") or ())
            ),
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
            freeze_declarations=tuple(freeze.get("declarations", ())),
            freeze_blueprint_nodes=tuple(freeze.get("blueprint_nodes", ())),
        )
