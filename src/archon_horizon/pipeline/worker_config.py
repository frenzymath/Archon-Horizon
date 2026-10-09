"""Explicit, closed worker configuration with no production-state discovery."""

from pathlib import Path
import os
import stat
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .models import SandboxPolicy
from .worker.contracts import validate_tool_environment
from .worker.lean_build import LeanBuildPolicy
from .worker.resource_health import ResourcePolicy
from .worker.cache_retention import (DEFAULT_MAX_BYTES, DEFAULT_MAX_AGE_SECONDS, DEFAULT_NATIVE_MAX_BYTES,
                                     DEFAULT_NATIVE_MIN_AGE_SECONDS)


class LeanBuildConfig(BaseModel):
    """Host build budgets, independent of the number of active agent sessions."""

    model_config = ConfigDict(extra="forbid")
    root: Path
    # Start with one memory-heavy Lean build at a time. Increase only after
    # measuring host capacity; agent slots do not imply extra compiler capacity.
    max_parallel_builds: int = Field(default=1, ge=1, le=64)
    max_parallel_preparations: int = Field(default=1, ge=1, le=64)
    # Defaults allow 30 min of compilation but only 30 s waiting for a build slot.
    timeout_seconds: float = Field(default=1800, gt=0, allow_inf_nan=False)
    queue_timeout_seconds: float = Field(default=30, ge=0, allow_inf_nan=False)
    # A 1 GiB free-space floor gates managed builds; it is not a workspace quota.
    minimum_free_bytes: int = Field(default=1024**3, ge=0)
    # Lake's artifact cache is host-local and shared by every worker using the
    # same lean_build.root.  Disable it only for an explicit isolation reason.
    artifact_cache: bool = True
    cache_max_bytes: int = Field(default=DEFAULT_MAX_BYTES, ge=0)
    cache_max_age_seconds: int = Field(default=DEFAULT_MAX_AGE_SECONDS, ge=0)
    native_cache_max_bytes: int = Field(default=DEFAULT_NATIVE_MAX_BYTES, ge=0)
    native_cache_min_age_seconds: int = Field(default=DEFAULT_NATIVE_MIN_AGE_SECONDS, ge=0)

    @model_validator(mode="after")
    def valid_policy(self):
        LeanBuildPolicy(**self.model_dump())
        return self


class LocalHarness(BaseModel):
    """An explicitly enrolled provider executable, state location and sandbox."""

    model_config = ConfigDict(extra="forbid")
    id: UUID
    adapter: Literal["codex_exec", "claude_exec"]
    executable: str
    provider_home: Path
    scratch_root: Path
    model: str | None = None
    reasoning_effort: str | None = None
    allowed_models: tuple[str, ...] | None = None
    allowed_reasoning_efforts: tuple[str, ...] | None = None
    provider_version: str | None = None
    adapter_version: str | None = None
    sandbox: SandboxPolicy
    environment: dict[str, str] = Field(default_factory=dict)
    max_requests_per_execution: int = Field(default=16, ge=1, le=1000)

    @field_validator("environment")
    @classmethod
    def tool_environment(cls, value):
        return validate_tool_environment(value)

    @field_validator("provider_home", "scratch_root")
    @classmethod
    def absolute(cls, value):
        if not value.is_absolute() or ".." in value.parts or value == Path("/"):
            raise ValueError("worker directories must be explicit absolute directories")
        return value


class WorkerConfig(BaseModel):
    """Validate the operator's worker JSON before opening credentials or journals.

    Unknown fields are rejected so misspelled policy settings cannot silently
    become defaults. See deploy/pipeline/worker.example.json for a concrete layout.
    """

    model_config = ConfigDict(extra="forbid")
    # Configuration shape version, independent of API v3 and package releases.
    schema_version: Literal[1] = 1
    host_id: UUID
    api_url: str
    agent_api_url: str | None = None
    token_file: Path
    journal_root: Path
    workspace_roots: list[Path] = Field(min_length=1)
    slots: int = Field(default=2, ge=1, le=128)
    journal_max_bytes: int = Field(default=512 * 1024**2, ge=1024**2)
    journal_min_free_bytes: int = Field(default=1024**3, ge=0)
    max_offline_replay_seconds: int = Field(default=7 * 86400, ge=1, le=365 * 86400)
    journal_diagnostic_max_bytes: int = Field(default=512 * 1024**2, ge=2048)
    journal_diagnostic_retention_seconds: int = Field(default=7 * 86400, ge=1)
    journal_failure_diagnostic_retention_seconds: int = Field(default=30 * 86400, ge=1)
    # One hour bounds a single provider turn; multiple turns still share the
    # execution budget below. These are configurable defaults, not lease lengths.
    max_request_seconds: int = Field(default=3600, ge=60, le=86400)
    # Wall-clock budget for one physical execution episode. Provider
    # continuations within the lease share this budget.
    max_execution_seconds: int = Field(default=4 * 3600, ge=60, le=7 * 86400)
    # Report this target to operators; admission uses journal/build byte floors.
    cleanup_target_free_percent: int = Field(
        default=20, ge=0, le=90,
        validation_alias=AliasChoices("cleanup_target_free_percent", "storage_reserve_percent"))
    harnesses: list[LocalHarness] = Field(min_length=1)
    publication_remotes: dict[UUID, str] = Field(default_factory=dict)
    publication_header_files: dict[UUID, Path] = Field(default_factory=dict)
    # Snapshot running work every five minutes for recovery. Publication polls
    # every five seconds in a separate lane, so it need not consume an agent slot.
    checkpoint_seconds: float = Field(default=300, ge=30, le=86400)
    publication_poll_seconds: float = Field(default=5, ge=1, le=300)
    publication_concurrency: int = Field(default=1, ge=1, le=4)
    lean_build: LeanBuildConfig | None = None
    lean_checks: bool = False
    # Compatibility flag also permits old roadmap-contract verification jobs.
    milestone_checks: bool = False
    resource_policy: ResourcePolicy = Field(default_factory=ResourcePolicy)

    @field_validator("api_url", "agent_api_url")
    @classmethod
    def api_origin(cls, value):
        """Require TLS for remote bearer-token transport; allow HTTP on loopback."""
        if value is None:
            return value
        parsed = urlsplit(value)
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or parsed.scheme not in ("http", "https")):
            raise ValueError("API URL must be an HTTP(S) origin without credentials")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("remote API connections require HTTPS")
        return value.rstrip("/")

    @field_validator("token_file", "journal_root")
    @classmethod
    def absolute(cls, value):
        if not value.is_absolute() or ".." in value.parts or value == Path("/"):
            raise ValueError("worker paths must be explicit absolute paths")
        return value

    @model_validator(mode="after")
    def unique_and_scoped(self):
        if (self.lean_checks or self.milestone_checks) and (self.lean_build is None or any(
                item.sandbox.mode != 'unrestricted' for item in self.harnesses)):
            raise ValueError('lean_checks/milestone_checks require a managed build policy and explicitly unrestricted build host')
        if len({item.id for item in self.harnesses}) != len(self.harnesses):
            raise ValueError("each local harness must have a unique id")
        for path in [*self.workspace_roots, *self.publication_header_files.values()]:
            self.absolute(path)
        # Check both ancestor directions: mounting a parent can expose protected
        # state just as directly as placing that state inside a workspace.
        for workspace in self.workspace_roots:
            for protected in [self.journal_root, self.token_file, *(item.provider_home for item in self.harnesses)]:
                if workspace == protected or workspace in protected.parents or protected in workspace.parents:
                    raise ValueError("workspaces must not overlap worker journal, credentials, or provider homes")
        if self.lean_build is not None:
            root = self.lean_build.root.resolve()
            protected = [self.journal_root, self.token_file, *self.workspace_roots,
                         *self.publication_header_files.values(),
                         *(item.provider_home for item in self.harnesses),
                         *(item.scratch_root for item in self.harnesses)]
            if any(root == path.resolve() or root in path.resolve().parents or path.resolve() in root.parents
                   for path in protected):
                raise ValueError("Lean build root must not overlap workspaces, credentials, scratch, or worker state")
            for item in self.harnesses:
                if item.sandbox.mode == "rootless_container" and not any(
                    Path(mount.source).resolve() == root and mount.target == "/horizon-build"
                    and mount.access == "read_write" for mount in item.sandbox.extra_mounts
                ):
                    raise ValueError("managed Lean builds require a read-write /horizon-build mount of the host build root")
        return self


def private_text(path):
    """Read one bounded secret line from a regular, owner-only credential file.

    O_NOFOLLOW rejects a symlink at the final path component. Validate the opened
    descriptor with fstat rather than checking a path and then reopening it, which
    would allow that final file to be swapped between the check and read.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError(f"Credential file must be private and owned by this user: {path}")
        # Read one character beyond the 64 Ki-character bound to detect oversize
        # input without reading an arbitrarily large file into memory.
        value = handle.read(65537).strip()
        if not value or len(value) > 65536 or "\n" in value or "\r" in value:
            raise ValueError(f"Credential file must contain one nonempty line: {path}")
        return value


def open_journal(config: WorkerConfig):
    from .worker.journal import DurableJournal
    return DurableJournal(config.journal_root, max_bytes=config.journal_max_bytes,
                          minimum_free_bytes=config.journal_min_free_bytes,
                          max_offline_seconds=config.max_offline_replay_seconds,
                          diagnostic_max_bytes=config.journal_diagnostic_max_bytes,
                          diagnostic_retention_seconds=config.journal_diagnostic_retention_seconds,
                          failure_diagnostic_retention_seconds=config.journal_failure_diagnostic_retention_seconds)


def load_worker(path: Path, *, progress_path: Path | None = None):
    from .worker.daemon import HarnessConfig, WorkerDaemon
    from .worker.provider import HeadlessAdapter
    from .worker.sandbox import SandboxMount, SandboxPolicy as RuntimeSandbox
    from .worker.transport import WorkerTransport

    config = WorkerConfig.model_validate_json(path.read_bytes())
    token = private_text(config.token_file)
    headers = {str(key): private_text(value) for key, value in config.publication_header_files.items()}
    transport = WorkerTransport(config.api_url, token)
    journal = open_journal(config)
    harnesses = {}
    if config.lean_build is not None:
        config.lean_build.root.mkdir(parents=True, exist_ok=True)
    for item in config.harnesses:
        sandbox = None
        if item.sandbox.mode == "rootless_container":
            sandbox = RuntimeSandbox(image_digest=item.sandbox.image_digest, network=item.sandbox.network,
                memory_limit_bytes=item.sandbox.memory_limit_bytes, cpu_limit=item.sandbox.cpu_limit,
                process_limit=item.sandbox.process_limit or 256,
                extra_mounts=tuple(SandboxMount(Path(mount.source), mount.target, mount.access == "read_only") for mount in item.sandbox.extra_mounts))
        harnesses[str(item.id)] = HarnessConfig(
            adapter=HeadlessAdapter(item.adapter, item.executable, item.model, item.reasoning_effort),
            provider_home=item.provider_home, scratch_root=item.scratch_root,
            sandbox=sandbox, unrestricted=item.sandbox.mode == "unrestricted",
            provider_version=item.provider_version, adapter_version=item.adapter_version,
            allowed_models=item.allowed_models, allowed_reasoning_efforts=item.allowed_reasoning_efforts,
            environment=dict(item.environment),
            lean_build=LeanBuildPolicy(**{**config.lean_build.model_dump(),
                "root": Path("/horizon-build") if sandbox else config.lean_build.root}) if config.lean_build else None,
            max_requests_per_execution=item.max_requests_per_execution)
    managed_storage_roots = (("lean_build", config.lean_build.root),) if config.lean_build else ()
    return WorkerDaemon(host_id=str(config.host_id), journal=journal, transport=transport,
        harnesses=harnesses, workspace_roots=tuple(config.workspace_roots),
        publication_remotes={str(key): value for key, value in config.publication_remotes.items()},
        publication_headers=headers,
        checkpoint_seconds=config.checkpoint_seconds,
        publication_poll_seconds=config.publication_poll_seconds,
        publication_concurrency=config.publication_concurrency,
        lean_checks=config.lean_checks, milestone_checks=config.milestone_checks,
        resource_policy=config.resource_policy,
        cleanup_target_free_percent=config.cleanup_target_free_percent,
        max_request_seconds=config.max_request_seconds,
        max_execution_seconds=config.max_execution_seconds,
        managed_storage_roots=managed_storage_roots,
        agent_api_url=config.agent_api_url, progress_path=progress_path), config.slots
