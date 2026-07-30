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


class ConfigError(ValueError):
    """A ``config.yaml`` is malformed in a way the user must fix.

    Subclasses ``ValueError`` so existing ``except ValueError`` call sites keep
    working; the CLI catches it to print the message instead of a traceback.
    """


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
        # ``kind`` is the one required key. Validate it here so a malformed
        # config yields an actionable message on *every* command instead of a
        # KeyError traceback (missing) or a late UnknownHarnessKind deep in the
        # registry (unquoted YAML ``null``, which parses as None -- exactly what
        # copying the documented ``kind: null`` example produces).
        if "kind" not in data:
            raise ConfigError(
                f"harness {name!r} is missing the required 'kind' key "
                "(expected one of: claude-code, codex, command, external-agent, \"null\")"
            )
        raw_kind = data["kind"]
        if raw_kind is None:
            raise ConfigError(
                f"harness {name!r} has kind: null, which YAML parses as an empty value. "
                'Quote it as kind: "null" to select the in-process null harness.'
            )
        kind = str(raw_kind).strip()
        if not kind:
            raise ConfigError(f"harness {name!r} has an empty 'kind'")
        return cls(
            name=name,
            kind=kind,
            command=data.get("command"),
            args=tuple(data.get("args", ())),
            model=data.get("model"),
            options=dict(data.get("options", {})),
        )

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
class BudgetConfig:
    """Optional spend ceilings for automated runs (``workspace.budget``).

    All limits are opt-in (unset = unlimited). A crossed limit stops the run
    cleanly — like a usage limit, state stays on disk and ``--resume`` picks it
    back up. ``session_tokens_out`` additionally cancels a live session that
    crosses it (checked from the session's live ``usage.json``).
    """

    session_tokens_out: int | None = None
    run_tokens_out: int | None = None
    run_cost_usd: float | None = None

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "BudgetConfig":
        def _int(key: str) -> int | None:
            value = data.get(key)
            return int(value) if value is not None else None

        cost = data.get("run_cost_usd")
        return cls(
            session_tokens_out=_int("session_tokens_out"),
            run_tokens_out=_int("run_tokens_out"),
            run_cost_usd=float(cost) if cost is not None else None,
        )

    @property
    def configured(self) -> bool:
        return any(v is not None for v in (self.session_tokens_out, self.run_tokens_out, self.run_cost_usd))


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
    blueprint_path: str | None = None
    build_command: str | None = None
    depends_on: tuple[str, ...] = ()
    freeze_files: tuple[str, ...] = ()
    freeze_declarations: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()

    @classmethod
    def from_raw(cls, name: str, data: dict[str, Any]) -> "ProjectConfig":
        # A `vcs:` block here is legacy and ignored: the workspace has ONE ledger and
        # projects have no repositories of their own.
        freeze_raw = data.get("freeze", {})
        return cls(
            name=name,
            path=data["path"],
            type=data.get("type", "lean"),
            blueprint_path=data.get("blueprint", {}).get("path"),
            build_command=data.get("build", {}).get("command"),
            depends_on=tuple(data.get("depends_on", ())),
            freeze_files=tuple(freeze_raw.get("files", ())),
            freeze_declarations=tuple(freeze_raw.get("declarations", ())),
            write_paths=tuple(data.get("write_paths", ())),
        )


@dataclass(frozen=True, slots=True)
class DelegationConfig:
    """Whether and how a running agent (a "team") may delegate by launching more
    work — new tasks, and if permitted, whole new ``horizon run`` sessions.

    The default is fully closed: an agent may NOT create tasks or launch runs.
    This block is the user's standing *consent record* that an agent reads before
    delegating. It is intentionally free-form — ``raw`` preserves every key the
    user wrote (e.g. account notes, limit-reset times, api-key hints) so the agent
    can read and reason about them — while only the load-bearing fields are typed.

    Reading this config never authorizes anything on its own; it is inert data.
    Any actuator that acts on it (spawning a run, selecting an account) is a
    separate, deliberately-gated capability.
    """
    allow_launch_tasks: bool = False   # may the agent create new tasks in the store
    allow_launch_runs: bool = False    # may the agent spawn a new `horizon run`
    max_parallel_sessions: int = 0     # cap on agent-launched concurrent runs (0 = none)
    accounts: tuple[Metadata, ...] = ()  # free-form account/api descriptors to choose among
    raw: Metadata = field(default_factory=dict)  # the whole block, verbatim, for the agent to read

    @classmethod
    def from_raw(cls, data: dict[str, Any]) -> "DelegationConfig":
        data = dict(data or {})
        accounts = tuple(
            dict(a) for a in (data.get("accounts") or ()) if isinstance(a, dict)
        )
        return cls(
            allow_launch_tasks=bool(data.get("allow_launch_tasks", False)),
            allow_launch_runs=bool(data.get("allow_launch_runs", False)),
            max_parallel_sessions=int(data.get("max_parallel_sessions", 0) or 0),
            accounts=accounts,
            raw=data,
        )


@dataclass(frozen=True, slots=True)
class WorkspaceConfig:
    name: str
    state_dir: str = ".archon-horizon"
    rounds: int = 1
    horizon_harness: str | None = None
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    budget: BudgetConfig = field(default_factory=BudgetConfig)
    delegation: DelegationConfig = field(default_factory=DelegationConfig)
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
            horizon_harness=ws.get("horizon_agent", {}).get("harness"),
            scheduler=SchedulerConfig.from_raw(ws.get("scheduler", {})),
            budget=BudgetConfig.from_raw(ws.get("budget", {}) or {}),
            delegation=DelegationConfig.from_raw(ws.get("delegation", {}) or {}),
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
