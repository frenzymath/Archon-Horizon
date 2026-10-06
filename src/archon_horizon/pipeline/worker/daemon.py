from __future__ import annotations

import os
import fcntl
import hashlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import errno
import json
import tempfile
import logging
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from tenacity import wait_random_exponential

from ..client import AgentClient
from ..activity_display import select_provider_event
from .contracts import ExecutionGrant, FencedExecution, JournalFull, Operation, validate_tool_environment
from .git_recovery import GitRecovery, PublicationBlocked
from .journal import DurableJournal, boot_identity
from .lean_build import LeanBuildPolicy
from .provider import HeadlessAdapter, PhysicalStopUnconfirmed, ProcessResult, ProcessSupervisor, terminate_owned_process, process_identity
from .sandbox import SandboxPolicy, podman_command
from .skills import materialize_bundle
from .storage_guard import inspect_storage
from .transport import WorkerTransport


@dataclass(frozen=True)
class HarnessConfig:
    adapter: HeadlessAdapter
    provider_home: Path
    scratch_root: Path
    sandbox: SandboxPolicy | None = None
    unrestricted: bool = False
    environment: dict[str, str] = field(default_factory=dict)
    max_requests_per_execution: int = 16
    provider_version: str | None = None
    adapter_version: str | None = None
    allowed_models: tuple[str, ...] | None = None
    allowed_reasoning_efforts: tuple[str, ...] | None = None
    lean_build: LeanBuildPolicy | None = None

    def __post_init__(self) -> None:
        if not self.provider_home.is_absolute() or not self.scratch_root.is_absolute():
            raise ValueError("provider home and scratch root must be explicit absolute paths")
        if self.sandbox is None and not self.unrestricted:
            raise ValueError("unrestricted execution must be explicitly enabled")
        if self.max_requests_per_execution < 1:
            raise ValueError("request budget must be positive")
        validate_tool_environment(self.environment)


class WorkerDaemon:
    """One local execution lane; shared journal claims support multiple lanes.

    All filesystem roots, harnesses and credentials come from operator config.
    Server grants select existing local capabilities and never supply a shell.
    """

    def __init__(self, *, host_id: str, journal: DurableJournal, transport: WorkerTransport,
                 harnesses: dict[str, HarnessConfig], workspace_roots: tuple[Path, ...],
                 publication_remotes: dict[str, str] | None = None,
                 publication_headers: dict[str, str] | None = None,
                 checkpoint_seconds: float = 300,
                 publication_poll_seconds: float = 5,
                 publication_concurrency: int = 1,
                 milestone_checks: bool = False,
                 cleanup_target_free_percent: int = 20,
                 max_request_seconds: float = 3600,
                 max_execution_seconds: float = 4 * 3600,
                 managed_storage_roots: tuple[tuple[str, Path], ...] = (),
                 agent_api_url: str | None = None,
                 progress_path: Path | None = None,
                 supervisor: ProcessSupervisor | None = None) -> None:
        if not harnesses or not workspace_roots or any(not p.is_absolute() for p in workspace_roots):
            raise ValueError("harnesses and explicit workspace roots are required")
        if type(cleanup_target_free_percent) is not int or not 0 <= cleanup_target_free_percent <= 90:
            raise ValueError("storage cleanup target percent must be an integer from 0 through 90")
        self.host_id = host_id
        self.journal = journal
        self.transport = transport
        self.harnesses = harnesses
        self.workspace_roots = tuple(p.resolve() for p in workspace_roots)
        self.cleanup_target_free_percent = cleanup_target_free_percent
        self.managed_storage_roots = tuple(managed_storage_roots)
        self.publication_remotes = dict(publication_remotes or {})
        self._publication_headers = dict(publication_headers or {})
        self.agent_api_url = agent_api_url or transport.base_url
        self.progress_path = progress_path or journal.state_root / "worker-progress.json"
        if not self.progress_path.is_absolute():
            raise ValueError("worker progress path must be absolute")
        if isinstance(max_execution_seconds, bool) or not isinstance(max_execution_seconds, (int, float)) \
                or not math.isfinite(max_execution_seconds) or max_execution_seconds <= 0:
            raise ValueError("execution budget must be positive")
        self.max_execution_seconds = max_execution_seconds
        self.supervisor = supervisor or ProcessSupervisor(journal, max_request_seconds=max_request_seconds)
        self._version_checks: set[tuple] = set()
        self._version_lock = threading.Lock()
        self._claim_lock = threading.Lock()
        self._api_replay_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._progress_components: dict[str, dict[str, Any]] = {}
        if checkpoint_seconds <= 0 or publication_poll_seconds <= 0 or not 1 <= publication_concurrency <= 4:
            raise ValueError("positive checkpoint/publication intervals and 1..4 publication lanes required")
        self.checkpoint_seconds = checkpoint_seconds
        self.publication_poll_seconds = publication_poll_seconds
        self.publication_concurrency = publication_concurrency
        self._publisher_running = False
        self.milestone_checks = milestone_checks
        if milestone_checks and any(config.sandbox is not None or config.lean_build is None for config in harnesses.values()):
            raise ValueError('Milestone verification requires an explicitly unrestricted managed build host')

    def storage_health(self) -> dict[str, Any]:
        """Return root-level storage health and the admission decision."""
        roots: list[tuple[str, Path]] = [("journal", self.journal.state_root)]
        roots.extend((f"workspace[{index}]", root) for index, root in enumerate(self.workspace_roots))
        for harness_id, config in sorted(self.harnesses.items()):
            roots.extend(((f"harness[{harness_id}].provider_home", config.provider_home),
                          (f"harness[{harness_id}].scratch", config.scratch_root)))
        roots.extend(self.managed_storage_roots)
        return inspect_storage(roots, cleanup_target_percent=self.cleanup_target_free_percent,
                               minimum_free_bytes=self.journal.minimum_free_bytes)

    def _progress(self, phase: str, *, deadline_seconds: float = 120, component: str | None = None,
                  details: dict[str, Any] | None = None) -> None:
        """Independent of journal/HTTP locks so an external watchdog can find stalls."""
        now, monotonic = time.time(), time.monotonic()
        with self._progress_lock:
            self._progress_components[component or threading.current_thread().name] = {
                "phase": phase, "updated_at": now, "monotonic": monotonic,
                "deadline_seconds": deadline_seconds,
                **({"details": details} if details is not None else {}),
            }
            value = {"schema_version": 1, "pid": os.getpid(), "boot_id": boot_identity(),
                     "process_identity": process_identity(os.getpid()), "updated_at": now,
                     "updated_monotonic": monotonic, "components": self._progress_components}
            path = self.progress_path
            temporary = path.with_suffix(".pending")
            with temporary.open("w") as output:
                os.chmod(temporary, 0o600)
                json.dump(value, output)
            os.replace(temporary, path)

    def _retire_progress(self, component: str) -> None:
        with self._progress_lock:
            self._progress_components.pop(component, None)

    @staticmethod
    def _local_failure(error: BaseException, phase: str) -> dict[str, str]:
        if isinstance(error, (JournalFull, OSError)) and (isinstance(error, JournalFull)
                or getattr(error, "errno", None) in {errno.ENOSPC, errno.EDQUOT}):
            kind, code = "storage", "storage_full"
        elif isinstance(error, subprocess.TimeoutExpired):
            kind, code = "transport", "local_operation_timeout"
        elif isinstance(error, (ValueError, KeyError)) and phase == "execution_initialization":
            kind, code = "configuration", "invalid_configuration"
        else:
            kind, code = "transport", "local_execution_unavailable"
        # Git/HTTP command arguments can contain credentials. Keep the phase and
        # exception type, not TimeoutExpired's argv or arbitrary subprocess text.
        detail = f"{phase}: {type(error).__name__}"
        if isinstance(error, OSError) and error.errno is not None:
            detail += f" (errno {error.errno})"
        if isinstance(error, subprocess.TimeoutExpired):
            detail += f" after {error.timeout}s"
        if isinstance(error, ValueError) and phase == "execution_initialization":
            detail += ": " + str(error)[:1000]
        return {"kind": kind, "code": code, "message": detail}

    @staticmethod
    def _orchestrator_tool_violation(event: dict[str, Any]) -> bool:
        """Best-effort detection for reactive cancellation, not pre-execution isolation."""
        if not isinstance(event, dict):
            return False
        item = event.get("item")
        if not isinstance(item, dict):
            return False
        if item.get("type") == "file_change":
            return True
        if item.get("type") != "command_execution":
            return False
        def invocation(argv: list[str], depth: int) -> bool:
            if depth > 4:
                return False
            while argv and "=" in argv[0] and argv[0].split("=", 1)[0].isidentifier():
                argv = argv[1:]
            if not argv:
                return False
            executable, args = Path(argv[0]).name, argv[1:]
            if executable in {"git", "lake", "lean"}:
                return True
            if executable in {"sh", "bash", "dash", "zsh", "ksh"}:
                for index, arg in enumerate(args):
                    if arg == "--command" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
                        return index + 1 < len(args) and shell(args[index + 1], depth + 1)
                    if not arg.startswith("-"):
                        break
            if executable in {"env", "command", "exec"}:
                while args and args[0].startswith("-"):
                    option, args = args[0], args[1:]
                    if option == "--":
                        break
                    if executable == "env" and option in {"-u", "--unset", "-C", "--chdir"}:
                        args = args[1:]
                return invocation(args, depth + 1)
            return False

        def shell(command: str, depth: int) -> bool:
            if depth > 4 or len(command) > 65536:
                return False
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()\n")
            lexer.whitespace = " \t\r"
            lexer.whitespace_split = True
            argv: list[str] = []
            try:
                for token in lexer:
                    if token and all(char in ";&|()\n" for char in token):
                        if invocation(argv, depth):
                            return True
                        argv = []
                    else:
                        argv.append(token)
            except ValueError:
                return False
            return invocation(argv, depth)

        return shell(str(item.get("command") or ""), 0)

    def _snapshot(self, recovery: GitRecovery) -> bool:
        try:
            self._progress("checkpoint", deadline_seconds=1800)
            recovery.checkpoint_dirty()
            recovery.reconcile()
            self.journal.record_recovery_health(recovery.execution_id, None)
            return True
        except (JournalFull, sqlite3.Error):
            raise
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            detail = self._local_failure(error, "checkpoint")
            self.journal.record_recovery_health(recovery.execution_id, detail["message"])
            return False

    @staticmethod
    def _defer_recovery(checkpoint: dict[str, Any]) -> None:
        attempts = checkpoint.get("recovery_attempts", 0) + 1
        delay = wait_random_exponential(multiplier=5, min=5, max=300)(
            SimpleNamespace(attempt_number=min(attempts, 32)))
        checkpoint.update(recovery_pending=True, recovery_attempts=attempts, recovery_retry_at=time.time() + delay)

    def _workspace(self, grant: ExecutionGrant) -> Path:
        workspace = Path(grant.workspace_path).resolve(strict=True)
        if not any(workspace == root or root in workspace.parents for root in self.workspace_roots):
            raise ValueError("server workspace is outside operator-enabled roots")
        if workspace == self.journal.state_root or self.journal.state_root in workspace.parents or workspace in self.journal.state_root.parents:
            raise ValueError("workspace overlaps daemon state")
        return workspace

    def _emit(self, grant: ExecutionGrant, kind: str, payload: dict[str, Any]) -> Operation:
        operation = Operation.create(grant.execution_id, grant.epoch, kind, payload)
        self.journal.enqueue(operation)
        return operation

    def _acknowledged(self, operation: Operation) -> bool:
        return any(row["operation_id"] == operation.operation_id and row["state"] == "acknowledged"
                   for row in self.journal.records())

    def _prepare_workspace(self, grant: ExecutionGrant, cancel=None) -> bool:
        if grant.workspace_preparation is None:
            return True
        from .workspaces import prepare_workspace
        self._progress("workspace_preparation", deadline_seconds=1000)
        next_renewal = 0.0

        def keep_alive():
            nonlocal next_renewal
            if cancel is not None and cancel.is_set():
                raise FencedExecution("workspace preparation cancelled")
            self.journal.assert_lease(grant.execution_id, grant.epoch)
            if time.monotonic() >= next_renewal:
                started = time.monotonic()
                try:
                    response = self.transport.heartbeat(grant.execution_id, grant.epoch)
                    if response.get("stop") or response.get("yield"):
                        raise FencedExecution("workspace preparation stopped by the control plane")
                    remaining = float(response["lease_seconds"]) - (time.monotonic() - started)
                    if remaining <= 0:
                        raise FencedExecution("workspace preparation lease expired during renewal")
                    self.journal.grant_lease(grant.execution_id, grant.epoch, remaining)
                except httpx.HTTPError:
                    self.journal.assert_lease(grant.execution_id, grant.epoch)
                next_renewal = time.monotonic() + max(.1, min(10, grant.lease_seconds / 3))

        def record_process(identity):
            self.journal.checkpoint(grant.execution_id, grant.epoch,
                {"workspace_preparation_pending": True, "recovery_pending": identity.get("pid") is not None, **identity})

        receipt = prepare_workspace(grant, self.workspace_roots, self.journal.state_root, keep_alive, record_process)
        operation = self._emit(grant, "workspace_prepared", receipt)
        deadline = time.monotonic() + min(30, grant.lease_seconds)
        while True:
            keep_alive()
            # Preparation has no background lease thread yet. Replay one bounded
            # HTTP attempt between renewals rather than draining another lane.
            self.transport.replay_one(self.journal)
            if self._acknowledged(operation):
                self.journal.checkpoint(grant.execution_id, grant.epoch, {})
                return True
            if time.monotonic() >= deadline:
                self.journal.fence(grant.execution_id, grant.epoch, "yielded")
                self._emit(grant, "execution_finished", {"status": "yielded", "reason": "workspace_receipt_unavailable"})
                self._flush()
                return False
            time.sleep(.25)

    @staticmethod
    def _validate_harness(grant: ExecutionGrant, config: HarnessConfig) -> HeadlessAdapter:
        WorkerDaemon._validate_sandbox(grant, config)
        pinned = grant.harness_configuration
        if pinned is None:
            if isinstance(config.adapter, HeadlessAdapter):
                config.adapter.validate(externally_isolated=config.sandbox is not None)
            return config.adapter
        if pinned.get("adapter") != config.adapter.provider:
            raise ValueError("local provider adapter differs from the pinned harness")
        options = pinned.get("model_options", {})
        for key in ("model", "reasoning_effort"):
            allowlist = config.allowed_models if key == "model" else config.allowed_reasoning_efforts
            requested = options.get(key)
            if allowlist is not None and requested not in allowlist:
                raise ValueError(f"pinned {key} is outside the configured provider capability")
            if allowlist is None and requested != getattr(config.adapter, key):
                raise ValueError(f"local {key} differs from the pinned harness")
        for key in ("provider_version", "adapter_version"):
            local = getattr(config, key)
            if local is not None and pinned.get(key) != local:
                raise ValueError(f"local {key} differs from the pinned harness")
        settings = pinned.get("settings", {})
        allowed = {"schema_version", "approval_mode", "sandbox_mode", "tool_names", "auto_compaction"}
        if not isinstance(settings, dict) or set(settings) - allowed:
            raise ValueError("unsupported pinned provider settings")
        adapter = replace(config.adapter, model=options.get("model"), reasoning_effort=options.get("reasoning_effort"),
                          approval_mode=settings.get("approval_mode", "deny"), sandbox_mode=settings.get("sandbox_mode", "workspace_write"),
                          tool_names=tuple(settings.get("tool_names", [])), auto_compaction=settings.get("auto_compaction", True),
                          max_parallel_subagents=grant.max_parallel_subagents)
        adapter.validate(externally_isolated=config.sandbox is not None)
        return adapter

    @staticmethod
    def _validate_sandbox(grant: ExecutionGrant, config: HarnessConfig) -> None:
        manifest = grant.sandbox_manifest
        if manifest is None:
            return
        allowed = {"schema_version", "mode", "image_digest", "network", "extra_mounts", "memory_limit_bytes", "cpu_limit", "process_limit"}
        if set(manifest) - allowed or manifest.get("schema_version", 1) != 1:
            raise ValueError("unsupported pinned sandbox manifest")
        mode = "rootless_container" if config.sandbox else "unrestricted"
        if manifest.get("mode") != mode:
            raise ValueError("local sandbox mode differs from the pinned execution policy")
        expected = {"image_digest": manifest.get("image_digest"), "network": manifest.get("network", "outbound"),
                    "memory_limit_bytes": manifest.get("memory_limit_bytes"), "cpu_limit": manifest.get("cpu_limit"),
                    "process_limit": manifest.get("process_limit") or (256 if config.sandbox else None)}
        actual = ({name: getattr(config.sandbox, name) for name in expected} if config.sandbox else
                  {"image_digest": None, "network": "outbound", "memory_limit_bytes": None, "cpu_limit": None, "process_limit": None})
        def mounts(rows):
            return sorted((str(Path(row["source"]).resolve()), str(Path(row["target"])), row["access"]) for row in rows)
        expected_mounts = mounts(manifest.get("extra_mounts", []))
        actual_mounts = mounts([{"source": str(item.source), "target": item.target,
                                 "access": "read_only" if item.read_only else "read_write"}
                                for item in config.sandbox.extra_mounts]) if config.sandbox else []
        if actual != expected or actual_mounts != expected_mounts:
            raise ValueError("local sandbox settings differ from the pinned execution policy")

    def _validate_executable(self, config: HarnessConfig) -> str | None:
        if config.sandbox is not None or not isinstance(config.adapter, HeadlessAdapter):
            return
        executable = shutil.which(config.adapter.executable, path=config.environment.get("PATH", os.defpath))
        if executable is None:
            raise ValueError("configured provider executable is missing")
        path = Path(executable).resolve(strict=True)
        if config.provider_version is None:
            return str(path)
        info = path.stat()
        key = (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, config.adapter.provider, config.provider_version)
        with self._version_lock:
            if key in self._version_checks:
                return str(path)
            env = {"PATH": os.defpath, "HOME": str(config.provider_home),
                   "CODEX_HOME": str(config.provider_home / ".codex"),
                   "CLAUDE_CONFIG_DIR": str(config.provider_home / ".claude"), **config.environment}
            result = subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10, env=env, check=False)
            pattern = r"codex(?:-cli)? ([^\s]+)" if config.adapter.provider == "codex_exec" else r"([^\s]+) \(Claude Code\)"
            match = re.fullmatch(pattern, result.stdout.strip()) if len(result.stdout) <= 4096 else None
            if result.returncode or not match or match[1] != config.provider_version:
                raise ValueError("actual provider executable version differs from the pinned configuration")
            self._version_checks.add(key)
        return str(path)

    @staticmethod
    def _podman_executable() -> str:
        executable = shutil.which("podman")
        if executable is None:
            raise ValueError("host Podman executable is missing")
        return str(Path(executable).resolve(strict=True))

    def _replay_agent_intents(self, grant: ExecutionGrant, agent_state: Path) -> dict[str, Any]:
        if not (agent_state / "api-intents.sqlite3").exists():
            return {"completed": [], "blocked": [], "pending": 0}
        if not grant.execution_token:
            return {"completed": [], "blocked": [{"message": "No current scoped execution credential"}], "pending": 1}
        client = None
        try:
            self.journal.assert_lease(grant.execution_id, grant.epoch)
            client = AgentClient(self.transport.base_url, grant.execution_token, grant.execution_id, agent_state,
                                 max_offline_seconds=grant.max_offline_replay_seconds, assignment_id=grant.assignment_id)
            return {**client.replay_pending(limit=20), "reconciliation": client.recovery_state()}
        except (sqlite3.Error, ValueError, OSError):
            return {"completed": [], "blocked": [{"message": "Agent intent journal requires repair"}], "pending": 1}
        finally:
            if client is not None:
                client.close()

    def _flush(self, limit: int = 100) -> None:
        self._progress("journal_replay", deadline_seconds=max(120, limit * 12))
        self.journal.prune_diagnostics()
        # Acknowledged transport receipts are replay-safe and can be removed
        # after a bounded age. Keep terminal execution receipts while their
        # provider diagnostics are retained by DurableJournal.compact().
        self.journal.compact(acknowledged_before=time.time() - 3600)
        if not self._publisher_running:
            self.preserve_one()
        # A provider boundary checks acknowledgement after this returns. Wait for
        # another lane's in-flight replay instead of mistaking it for a lost ack.
        with self._api_replay_lock:
            for _ in range(limit):
                disposition = self.transport.replay_one(self.journal)
                if disposition is None or disposition == "pending":
                    return

    def preserve_one(self, *, now: float | None = None) -> str | None:
        now = time.time() if now is None else now
        claimed = self.journal.claim(now=now, claim_seconds=180, destination="local_git")
        if claimed is None:
            return None
        stopped = threading.Event()
        renew_errors: list[BaseException] = []

        def renew_claim() -> None:
            while not stopped.wait(30):
                try:
                    if not self.journal.renew_delivery(claimed):
                        return
                except BaseException as error:
                    renew_errors.append(error)
                    return

        renewal = threading.Thread(target=renew_claim, name="horizon-publication-lease")
        renewal.start()
        try:
            return self._preserve_claim(claimed, now=now)
        finally:
            stopped.set()
            renewal.join()
            if renew_errors:
                raise renew_errors[0]

    def _preserve_claim(self, claimed, *, now: float) -> str:
        item = claimed.operation
        payload = item.payload
        remote = self.publication_remotes.get(payload["repository_id"])
        try:
            if remote is None:
                raise PublicationBlocked("publication_remote_not_configured")
            workspace = Path(payload["workspace_path"]).resolve(strict=True)
            if not any(workspace == root or root in workspace.parents for root in self.workspace_roots):
                raise ValueError("publication workspace escaped configured roots")
            recovery = GitRecovery(workspace, payload["repository_id"], item.execution_id, item.epoch, self.journal,
                                   publication_header=self._publication_headers.get(payload["repository_id"]))
            ref = recovery.preserve_remote(payload["commit_oid"], remote=remote)
        except (PublicationBlocked, ValueError, FileNotFoundError, PermissionError) as error:
            code = error.code if isinstance(error, PublicationBlocked) else "git_local_configuration"
            key = str(uuid.uuid5(uuid.NAMESPACE_URL, item.operation_id + ":failed:" + code))
            failure = self.journal.operation(key) or Operation.create(item.execution_id, item.epoch, "publication_failed",
                {"repository_id": payload["repository_id"], "commit_oid": payload["commit_oid"],
                 "recovery_ref": payload["recovery_ref"], "failure_code": code}, operation_id=key, occurred_at=item.occurred_at)
            self.journal.enqueue(failure)
            self.journal.settle(claimed, "blocked", error=code)
            return "blocked"
        except (RuntimeError, subprocess.TimeoutExpired, OSError):
            delay = wait_random_exponential(multiplier=5, min=1, max=300)(SimpleNamespace(attempt_number=min(claimed.attempts, 32)))
            self.journal.settle(claimed, "pending", retry_at=now + delay, error="git_preservation_unavailable")
            return "pending"
        verified_key = str(uuid.uuid5(uuid.NAMESPACE_URL, item.operation_id + ":verified"))
        verified = self.journal.operation(verified_key) or Operation.create(item.execution_id, item.epoch, "publication_verified",
                                    {"repository_id": payload["repository_id"], "commit_oid": payload["commit_oid"],
                                     "recovery_ref": payload["recovery_ref"], "remote_ref": ref, "remote": remote},
                                    operation_id=verified_key, occurred_at=item.occurred_at)
        self.journal.enqueue(verified)
        self.journal.settle(claimed, "acknowledged", now=now)
        return "acknowledged"

    def _checkpoint_loop(self, recovery: GitRecovery, stopped: threading.Event,
                         process_cancel: threading.Event, failures: list[BaseException]) -> None:
        while not stopped.wait(self.checkpoint_seconds):
            try:
                # This is a recoverable file snapshot, not a transaction across
                # files an active provider may be editing. Never touch its index.
                recovery.checkpoint_dirty()
                recovery.record_head()
                self.journal.record_recovery_health(recovery.execution_id, None)
            except (JournalFull, sqlite3.Error) as error:
                failures.append(error)
                process_cancel.set()
                return
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
                    failures.append(JournalFull("Git checkpoint storage exhausted"))
                    process_cancel.set()
                    return
                try:
                    self.journal.record_recovery_health(recovery.execution_id,
                        self._local_failure(error, "periodic_checkpoint")["message"])
                except BaseException as health_error:
                    failures.append(health_error)
                    process_cancel.set()
                    return

    @staticmethod
    def _stop_container(name: str) -> None:
        if not name.startswith("horizon-"):
            raise ValueError("refusing to remove an unowned container")
        result = subprocess.run([WorkerDaemon._podman_executable(), "rm", "--force", "--ignore", name],
                                capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError("sandbox cleanup failed; retain execution recovery state")

    def recover(self, *, include_running: bool = True) -> list[str]:
        if not self._recovery_lock.acquire(blocking=False):
            return []
        try:
            return self._recover(include_running=include_running)
        finally:
            self._recovery_lock.release()

    def _recover(self, *, include_running: bool) -> list[str]:
        recovered: list[str] = []
        for row in self.journal.executions():
            if row["status"] != "running" and not row["checkpoint"].get("recovery_pending"):
                continue
            if row["status"] == "running" and not include_running:
                try:
                    self.journal.assert_lease(row["execution_id"], row["epoch"])
                except FencedExecution:
                    pass
                else:
                    continue
            checkpoint = row["checkpoint"]
            if checkpoint.get("recovery_retry_at", 0) > time.time():
                continue
            try:
                if self._recover_execution(row, checkpoint):
                    recovered.append(row["execution_id"])
            except (JournalFull, sqlite3.Error):
                raise
            except BlockingIOError:
                # Another lane still owns the checkout; do not replace its
                # current provider checkpoint with this earlier observation.
                continue
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
                self._defer_recovery(checkpoint)
                self.journal.checkpoint(row["execution_id"], row["epoch"], checkpoint)
                self.journal.record_recovery_health(row["execution_id"],
                    self._local_failure(error, "recovery")["message"])
        return recovered

    def _recover_execution(self, row: dict[str, Any], checkpoint: dict[str, Any]) -> bool:
        workspace = Path(checkpoint["workspace_path"]).resolve(strict=True) if checkpoint.get("workspace_path") else None
        locks = self.journal.state_root / "workspace-locks"
        locks.mkdir(mode=0o700, exist_ok=True)
        lock_name = hashlib.sha256(str(workspace or row["execution_id"]).encode()).hexdigest()
        with (locks / lock_name).open("a") as lock:
            # A running lane may still be persisting its terminal checkpoint.
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            row = next(current for current in self.journal.executions()
                       if current["execution_id"] == row["execution_id"])
            checkpoint = row["checkpoint"]
            if row["status"] != "running" and not checkpoint.get("recovery_pending"):
                return False
            terminate_owned_process(checkpoint)
            if checkpoint.get("container_name"):
                self._stop_container(checkpoint["container_name"])
            status = "lost" if row["status"] == "running" else row["status"]
            self.journal.fence(row["execution_id"], row["epoch"], status)
            if checkpoint.get("request_id"):
                request = self.journal.request(checkpoint["request_id"])
                if request and request["state"] == "uncertain":
                    directory = self.journal.state_root / "requests" / checkpoint["request_id"]
                    result = ProcessResult("lost", -9, checkpoint.get("provider_thread_id"),
                                           str(directory / "stdout.jsonl"), str(directory / "stderr.log"),
                                           0, "worker_restarted")
                    self.journal.finish_request(checkpoint["request_id"], state="interrupted",
                                                provider_thread_id=checkpoint.get("provider_thread_id"),
                                                result=result.__dict__)
            if checkpoint.get("workspace_path") and checkpoint.get("repository_id"):
                assert workspace is not None
                if not any(workspace == root or root in workspace.resolve().parents for root in self.workspace_roots):
                    raise ValueError("recovery workspace escaped configured roots")
                recovery = GitRecovery(workspace, checkpoint["repository_id"], row["execution_id"], row["epoch"], self.journal)
                if not self._snapshot(recovery):
                    self._defer_recovery(checkpoint)
                    self.journal.checkpoint(row["execution_id"], row["epoch"], checkpoint)
                    return False
            terminal = checkpoint.get("terminal_operation")
            if terminal is None:
                terminal = Operation.create(row["execution_id"], row["epoch"], "execution_finished",
                    {"status": status, "reason": "worker_restarted",
                     "provider_thread_id": checkpoint.get("provider_thread_id")}).as_dict()
                checkpoint["terminal_operation"] = terminal
                self.journal.checkpoint(row["execution_id"], row["epoch"], checkpoint)
            self.journal.enqueue(Operation(**terminal))
            checkpoint.update(recovery_pending=False, pid=None, process_identity=None,
                              recovery_attempts=0, recovery_retry_at=0)
            self.journal.checkpoint(row["execution_id"], row["epoch"], checkpoint)
            return True

    def run_once(self, *, cancel: threading.Event | None = None) -> str | None:
        self._progress("recovery", deadline_seconds=1800)
        self.recover(include_running=False)
        self._flush()
        storage = self.storage_health()
        if storage["status"] == "storage_pressure":
            self._progress("storage_pressure", details=storage)
            return None
        if not self.journal.diagnostic_capacity(self.supervisor.max_log_bytes * 2):
            self._progress("diagnostic_storage_pressure", details={
                "free_bytes": shutil.disk_usage(self.journal.state_root).free,
                "required_free_bytes": self.journal.minimum_free_bytes + self.supervisor.max_log_bytes * 2,
                "diagnostic_max_bytes": self.journal.diagnostic_max_bytes})
            return None
        with self._claim_lock:
            self._progress("claim", deadline_seconds=120)
            attempt = self.journal.claim_attempt(self.host_id, list(self.harnesses))
            started = time.monotonic()
            grant = self.transport.claim(**attempt["payload"], request_id=attempt["request_id"])
            remaining = grant.lease_seconds - (time.monotonic() - started) if grant else 0
            fresh = self.journal.resolve_claim(attempt["request_id"], execution_id=grant.execution_id if grant else None,
                                                epoch=grant.epoch if grant else 1, seconds=remaining)
        if grant is None or not fresh:
            return None
        if remaining <= 0:
            self._emit(grant, "execution_finished", {"status": "lost", "reason": "claim_expired_before_launch"})
            self._flush()
            return "lost"
        try:
            self._validate_harness(grant, self.harnesses[grant.harness_id])
            self._validate_executable(self.harnesses[grant.harness_id])
            if not self._prepare_workspace(grant, cancel):
                return "yielded"
            self._workspace(grant)
        except FencedExecution:
            self.journal.fence(grant.execution_id, grant.epoch)
            self._emit(grant, "execution_finished", {"status": "lost", "reason": "workspace_preparation_fenced"})
            self._flush()
            return "lost"
        except (ValueError, KeyError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            self.journal.fence(grant.execution_id, grant.epoch, "failed")
            self._emit(grant, "execution_finished", {"status": "failed", "failure":
                self._local_failure(error, "execution_initialization")})
            self._flush()
            return "failed"
        return self.execute(grant, cancel=cancel)

    def execute(self, grant: ExecutionGrant, *, cancel: threading.Event | None = None) -> str:
        try:
            workspace = self._workspace(grant)
            if any(row["execution_id"] != grant.execution_id and row["checkpoint"].get("recovery_pending")
                   and row["checkpoint"].get("workspace_path") == str(workspace)
                   for row in self.journal.executions()):
                raise RuntimeError("workspace has pending preservation or process cleanup")
            locks = self.journal.state_root / "workspace-locks"
            locks.mkdir(mode=0o700, exist_ok=True)
            lock_name = hashlib.sha256(str(workspace).encode()).hexdigest()
            with (locks / lock_name).open("a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise ValueError("physical workspace is already owned by another execution") from error
                return self._execute(grant, cancel=cancel)
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            row = next(item for item in self.journal.executions() if item["execution_id"] == grant.execution_id)
            checkpoint = row["checkpoint"]
            if checkpoint.get("initialized") or checkpoint.get("request_id") or checkpoint.get("container_name"):
                raise
            self.journal.fence(grant.execution_id, grant.epoch, "failed")
            self._emit(grant, "execution_finished", {"status": "failed", "failure":
                self._local_failure(error, "execution_initialization")})
            checkpoint["recovery_pending"] = False
            self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
            self._flush()
            return "failed"

    def _execute(self, grant: ExecutionGrant, *, cancel: threading.Event | None = None) -> str:
        execution_started = time.monotonic()
        if not grant.goal and grant.goal_artifact_id:
            try:
                grant = replace(grant, goal=self.transport.goal(grant))
            except httpx.HTTPError:
                self.journal.fence(grant.execution_id, grant.epoch, "yielded")
                self._emit(grant, "execution_finished", {"status": "yielded", "reason": "goal_fetch_unavailable"})
                self._flush()
                return "yielded"
        workspace = self._workspace(grant)
        config = self.harnesses[grant.harness_id]
        adapter = self._validate_harness(grant, config)
        orchestrator = "orchestrator" in grant.functions
        if orchestrator:
            adapter = replace(adapter, max_parallel_subagents=0)
        executable = self._validate_executable(config)
        if executable is not None:
            adapter = replace(adapter, executable=executable)
        skill_bundle = None
        if grant.skill_bundle_sha256:
            try:
                skill_bundle = materialize_bundle(self.journal.state_root, grant.skill_bundle_sha256,
                    grant.skill_bundle if grant.skill_bundle is not None else self.transport.skill_bundle(grant.execution_id))
            except httpx.HTTPError:
                self.journal.fence(grant.execution_id, grant.epoch, "yielded")
                self._emit(grant, "execution_finished", {"status": "yielded", "reason": "skill_bundle_fetch_unavailable"})
                self._flush()
                return "yielded"
            except (ValueError, OSError) as error:
                self.journal.fence(grant.execution_id, grant.epoch, "failed")
                self._emit(grant, "execution_finished", {"status": "failed", "failure":
                    self._local_failure(error, "execution_initialization")})
                self._flush()
                return "failed"
        config.provider_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        agent_state = config.provider_home / "assignments" / grant.assignment_id / "api-intents"
        agent_state.mkdir(mode=0o700, parents=True, exist_ok=True)
        if agent_state.resolve() != config.provider_home.resolve() / "assignments" / grant.assignment_id / "api-intents":
            raise ValueError("agent intent storage is redirected outside its assignment directory")
        scratch = config.scratch_root / grant.execution_id
        scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        if scratch.resolve() != config.scratch_root.resolve() / grant.execution_id:
            raise ValueError("execution scratch is redirected outside its assigned directory")
        reviewer_accounts_file = scratch / "reviewer-accounts.json"
        provider_thread_id = grant.provider_thread_id
        checkpoint: dict[str, Any] = {
            "workspace_path": str(workspace), "repository_id": grant.repository_id,
            "workspace_id": grant.workspace_id, "provider_thread_id": provider_thread_id,
            "provider_thread_record_id": grant.provider_thread_record_id,
            "harness_id": grant.harness_id,
            "recovery_pending": True,
        }
        recovery = GitRecovery(workspace, grant.repository_id, grant.execution_id, grant.epoch, self.journal)
        self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
        stopped = threading.Event()
        process_cancel = threading.Event()
        command_lock = threading.Lock()
        latest: dict[str, Any] = {"mission_revision_number": grant.mission_revision_number,
                                  "run_revision": grant.run_revision}
        heartbeat_error: list[BaseException] = []

        def renew() -> dict[str, Any]:
            sent_at = time.monotonic()
            response = self.transport.heartbeat(grant.execution_id, grant.epoch,
                                                provider_thread_id=provider_thread_id)
            lease_seconds = float(response["lease_seconds"]) - (time.monotonic() - sent_at)
            if lease_seconds <= 0:
                raise FencedExecution("renewal response arrived after its lease window")
            self.journal.grant_lease(grant.execution_id, grant.epoch, lease_seconds)
            with command_lock:
                stale = any(response.get(key, latest[key]) < latest[key]
                            for key in ("mission_revision_number", "run_revision"))
                if stale:
                    response = {key: value for key, value in response.items()
                                if key not in {"goal", "continue", "mission_revision_id", "mission_revision_number",
                                               "run_revision", "roadmap_snapshot_id"}}
                latest.update(response)
                decision = dict(latest)
            if response.get("stop") or response.get("yield"):
                process_cancel.set()
            return decision

        def heartbeat_loop() -> None:
            interval = max(0.1, min(30, grant.lease_seconds / 3))
            while not stopped.wait(interval):
                if cancel is not None and cancel.is_set():
                    process_cancel.set()
                try:
                    self._progress("heartbeat", component=f"heartbeat:{grant.execution_id}", deadline_seconds=90)
                    renew()
                except FencedExecution as error:
                    heartbeat_error.append(error)
                    self.journal.fence(grant.execution_id, grant.epoch)
                    process_cancel.set()
                    return
                except (httpx.HTTPError, KeyError, ValueError):
                    # API outage never extends the previous monotonic lease.
                    continue

        heartbeat_thread = threading.Thread(target=heartbeat_loop, name="horizon-lease-renewal", daemon=True)
        heartbeat_thread.start()
        status = "failed"
        reason: str | None = None
        failure: dict[str, str] | None = None
        intent_reconciliation = None
        cleanup_failed = False
        preservation_ok = True
        prompt = grant.goal
        goal_revision = {"mission_revision_id": grant.mission_revision_id,
                         "mission_revision_number": grant.mission_revision_number,
                         "run_revision": grant.run_revision, "roadmap_snapshot_id": grant.roadmap_snapshot_id}
        try:
            recovery.reconcile()
            checkpoint["initialized"] = True
            self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
            for number in range(config.max_requests_per_execution):
                remaining_execution = self.max_execution_seconds - (time.monotonic() - execution_started)
                if remaining_execution <= 0:
                    status, reason = "yielded", "execution_budget_reached"
                    break
                if not self.journal.diagnostic_capacity(self.supervisor.max_log_bytes * 2):
                    status, reason = "yielded", "diagnostic_storage_pressure"
                    break
                if cancel is not None and cancel.is_set():
                    status, reason = "yielded", "daemon_stopping"
                    break
                self.journal.assert_lease(grant.execution_id, grant.epoch)
                accounts = self.transport.reviewer_accounts(grant)
                fd, temporary = tempfile.mkstemp(prefix=".reviewer-accounts-", dir=scratch)
                try:
                    with os.fdopen(fd, "w") as stream:
                        json.dump(accounts, stream)
                    os.replace(temporary, reviewer_accounts_file)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                with command_lock:
                    for key in goal_revision:
                        if key in latest:
                            goal_revision[key] = latest[key]
                    prompt = latest.get("goal") or prompt
                request_revision = dict(goal_revision)
                request_id = str(uuid.uuid4())
                started_operation = self._emit(grant, "provider_observed", {
                    "event": "request_started", "request_id": request_id,
                    "provider_thread_record_id": grant.provider_thread_record_id,
                    "provider_thread_id": provider_thread_id,
                    "goal": prompt, **request_revision,
                })
                self._flush()
                if not self._acknowledged(started_operation):
                    status, reason = "yielded", "request_admission_not_acknowledged"
                    break
                command_options = {"provider_thread_id": provider_thread_id,
                                   "externally_isolated": config.sandbox is not None}
                if (config.sandbox is None and adapter.provider == "codex_exec"
                        and getattr(adapter, "sandbox_mode", None) == "workspace_write"):
                    command_options["agent_state_path"] = agent_state.resolve()
                    if config.lean_build is not None:
                        command_options["tool_writable_roots"] = (config.lean_build.root.resolve(),)
                command = adapter.command(**command_options)
                container_name: str | None = None
                build_environment = config.lean_build.environment(grant.harness_id) if config.lean_build else {}
                if config.sandbox is not None:
                    container_name = f"horizon-{grant.execution_id}-{grant.epoch}-{number}"
                    checkpoint["container_name"] = container_name
                    self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
                    command = podman_command(config.sandbox, workspace=workspace, provider_home=config.provider_home,
                                             scratch=scratch, protected_roots=(self.journal.state_root,),
                                             command=command, name=container_name,
                                             skill_bundle=skill_bundle,
                                             podman_executable=self._podman_executable(),
                                             environment_values=config.environment,
                                             workspace_read_only=orchestrator,
                                             environment_names=tuple(config.environment) + tuple(build_environment) + (
                                                 "HORIZON_API_URL", "HORIZON_EXECUTION_TOKEN",
                                                 "HORIZON_EXECUTION_ID", "HORIZON_ASSIGNMENT_ID", "HORIZON_AGENT_STATE",
                                                 "HORIZON_MAX_OFFLINE_REPLAY_SECONDS", "HORIZON_PROVIDER_REQUEST_ID",
                                                 "HORIZON_PROVIDER_THREAD_ID", "HORIZON_SKILLS_DIR",
                                                 "HORIZON_REVIEWER_ACCOUNTS_FILE"))
                env = {"PATH": os.defpath, "HOME": str(config.provider_home), "TMPDIR": str(scratch),
                       "TMP": str(scratch), "TEMP": str(scratch),
                       "CODEX_HOME": str(config.provider_home / ".codex"),
                       "CLAUDE_CONFIG_DIR": str(config.provider_home / ".claude"), **config.environment, **build_environment,
                       "HORIZON_API_URL": self.agent_api_url,
                       "HORIZON_EXECUTION_TOKEN": grant.execution_token or "",
                       "HORIZON_EXECUTION_ID": grant.execution_id,
                       "HORIZON_ASSIGNMENT_ID": grant.assignment_id,
                       "HORIZON_PROVIDER_REQUEST_ID": request_id,
                       "HORIZON_PROVIDER_THREAD_ID": grant.provider_thread_record_id or "",
                       "HORIZON_SKILLS_DIR": "/horizon-skills" if config.sandbox and skill_bundle else str(skill_bundle or ""),
                       "HORIZON_REVIEWER_ACCOUNTS_FILE": "/tmp/reviewer-accounts.json" if config.sandbox else str(reviewer_accounts_file),
                       "HORIZON_MAX_OFFLINE_REPLAY_SECONDS": str(grant.max_offline_replay_seconds),
                       "HORIZON_AGENT_STATE": str(Path("/provider-home") / agent_state.relative_to(config.provider_home))
                           if config.sandbox else str(agent_state)}
                if config.sandbox is not None:
                    # Podman resolves its helpers and image storage on the host;
                    # image tool paths are explicit --env values in its argv.
                    env.update(PATH=os.environ.get("PATH", os.defpath), HOME=str(Path.home()))
                    for key in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
                        if key in os.environ:
                            env[key] = os.environ[key]
                        else:
                            env.pop(key, None)

                def observe(event: dict[str, Any]) -> None:
                    if orchestrator and self._orchestrator_tool_violation(event):
                        process_cancel.set()
                    provider = adapter.provider.removesuffix("_exec")
                    selected = select_provider_event(provider, event)
                    if selected is not None:
                        envelope = {"event": "native_event", "request_id": request_id,
                                    "provider_thread_record_id": grant.provider_thread_record_id,
                                    "adapter": provider, "raw": selected}
                        try:
                            self._emit(grant, "provider_observed", envelope)
                        except JournalFull:
                            # Display telemetry must not prevent lease/stop receipts.
                            # Retained stdout remains available to activity replay.
                            from ..provider_events import select_native_event
                            native = select_native_event(provider, event)
                            if native is not None:
                                self._emit(grant, "provider_observed", {**envelope, "raw": native})

                checkpoint_stopped = threading.Event()
                checkpoint_failures: list[BaseException] = []
                checkpoint_thread = threading.Thread(target=self._checkpoint_loop,
                    args=(recovery, checkpoint_stopped, process_cancel, checkpoint_failures),
                    name="horizon-checkpoint")
                checkpoint_thread.start()
                try:
                    lifecycle_observer = None
                    if adapter.provider.removesuffix("_exec") == "codex":
                        from .codex_lifecycle import CodexLifecycleObserver
                        lifecycle_observer = CodexLifecycleObserver(self.journal, config.provider_home,
                            request_id=request_id, execution_id=grant.execution_id, epoch=grant.epoch,
                            thread_record_id=grant.provider_thread_record_id, since=time.time(),
                            native_thread_id=provider_thread_id)
                    # Recompute after workspace/reviewer/account setup. The
                    # budget covers the physical execution episode, not merely
                    # provider subprocess time.
                    remaining_execution = self.max_execution_seconds - (time.monotonic() - execution_started)
                    if remaining_execution <= 0:
                        process_cancel.set()
                        status, reason = "yielded", "execution_budget_reached"
                        break
                    request_supervisor = self.supervisor
                    # The request deadline is subordinate to the cumulative
                    # execution episode budget. This prevents a continuation chain
                    # from consuming N * max_request_seconds while retaining
                    # the workspace and provider context.
                    if request_supervisor.max_request_seconds > remaining_execution:
                        request_supervisor = ProcessSupervisor(
                            self.journal, max_log_bytes=self.supervisor.max_log_bytes,
                            max_request_seconds=max(0.1, remaining_execution),
                            poll_seconds=self.supervisor.poll_seconds)
                    execution_deadline = remaining_execution <= min(
                        self.supervisor.max_request_seconds, 300 if orchestrator else float("inf"))
                    if orchestrator and request_supervisor.max_request_seconds > 300:
                        # Control episodes must remain short even when the
                        # substantive worker budget is large.  A supervisor
                        # restart or a slow API cannot consume a full worker
                        # lease while the orchestrator holds the slot.
                        request_supervisor = ProcessSupervisor(
                            self.journal, max_log_bytes=self.supervisor.max_log_bytes,
                            max_request_seconds=min(300, remaining_execution),
                            poll_seconds=self.supervisor.poll_seconds)
                    self._progress("provider_running", deadline_seconds=request_supervisor.max_request_seconds + 120)
                    result = request_supervisor.run(command, prompt=prompt, request_id=request_id,
                                                 execution_id=grant.execution_id, epoch=grant.epoch,
                                                 workspace=workspace, env=env,
                                                 provider_thread_id=provider_thread_id, cancel=process_cancel,
                                                 checkpoint=checkpoint, on_event=observe,
                                                 lifecycle_observer=lifecycle_observer)
                finally:
                    checkpoint_stopped.set()
                    checkpoint_thread.join()
                    if container_name is not None:
                        try:
                            self._stop_container(container_name)
                        except (OSError, RuntimeError, subprocess.TimeoutExpired):
                            cleanup_failed = True
                            raise
                if checkpoint_failures:
                    raise checkpoint_failures[0]
                provider_thread_id = result.provider_thread_id
                checkpoint["provider_thread_id"] = provider_thread_id
                completed_operation = self._emit(grant, "provider_observed", {"event": "request_completed", "request_id": request_id,
                           "provider_thread_record_id": grant.provider_thread_record_id,
                           "provider_thread_id": provider_thread_id, "status": result.status,
                           "returncode": result.returncode, "omitted_bytes": result.omitted_bytes,
                           **({"failure": result.failure} if result.failure and result.status == "failed" else {}),
                           **request_revision})
                # Preserve the native outcome even when a subsequent snapshot fails.
                status, reason, failure = result.status, result.reason, result.failure
                if execution_deadline and result.reason == "request_deadline":
                    # This deadline was imposed by the execution episode
                    # budget. Surface a durable yield so the server can hand
                    # the context to a planner instead of replaying a failure.
                    status, reason = "yielded", "execution_budget_reached"
                if result.status != "succeeded":
                    if latest.get("yield"):
                        status, reason = "yielded", "control_plane_yield"
                    elif cancel is not None and cancel.is_set() and not latest.get("stop"):
                        status, reason = "yielded", "daemon_stopping"
                preservation_ok = self._snapshot(recovery)
                if not preservation_ok:
                    if status == "succeeded":
                        status, reason = "yielded", "checkpoint_pending"
                    break
                if result.status != "succeeded":
                    if status == "yielded" and latest.get("yield"):
                        # A no-progress checkpoint can interrupt a legacy context
                        # before it repairs its journal. Reconcile under the still
                        # live lease so definitive old rejects can settle normally.
                        replay = self._replay_agent_intents(grant, agent_state)
                        intent_reconciliation = replay.get("reconciliation")
                        if replay["blocked"] or replay["pending"]:
                            reason = "agent_intents_require_reconciliation"
                    break
                self._flush()
                if not self._acknowledged(completed_operation):
                    status, reason = "yielded", "request_completion_not_acknowledged"
                    break
                replay = self._replay_agent_intents(grant, agent_state)
                intent_reconciliation = replay.get("reconciliation")
                if replay["blocked"] or replay["pending"]:
                    status, reason = "yielded", "agent_intents_require_reconciliation"
                    break
                response = renew()
                if response.get("stop"):
                    status, reason = "cancelled", "control_plane_cancelled"
                    break
                if response.get("yield"):
                    status, reason = "yielded", "control_plane_yield"
                    break
                if response.get("continue") is False:
                    status = "succeeded"
                    break
                if response.get("continue") is not True or not provider_thread_id:
                    status, reason = "yielded", "continuation_requires_reconciliation"
                    break
                prompt = response.get("goal") or "Continue the existing mission and address its remaining open obligations."
            else:
                status, reason = "yielded", "request_budget_reached"
        except FencedExecution:
            status, reason = "lost", "lease_fenced"
        except httpx.HTTPError:
            status, reason = "yielded", "control_plane_unavailable_at_boundary"
        except JournalFull:
            status, reason = "failed", "local_storage_full"
            failure = {"kind": "storage", "code": "storage_full", "message": "Worker durable storage needs operator repair"}
        except PhysicalStopUnconfirmed:
            status, reason, cleanup_failed = "lost", "physical_stop_unconfirmed", True
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            status, reason = "failed", "local_execution_failed"
            failure = self._local_failure(error, "local_execution")
        finally:
            try:
                reviewer_accounts_file.unlink(missing_ok=True)
            except OSError:
                logging.warning("Private reviewer credential cleanup needs host attention")
            persisted = next(row["checkpoint"] for row in self.journal.executions()
                             if row["execution_id"] == grant.execution_id)
            provider_thread_id = persisted.get("provider_thread_id") or provider_thread_id
            checkpoint = {**persisted, **checkpoint, "provider_thread_id": provider_thread_id}
            try:
                if not cleanup_failed and preservation_ok:
                    preservation_ok = self._snapshot(recovery)
            finally:
                stopped.set()
                heartbeat_thread.join(timeout=12)
                self._retire_progress(f"heartbeat:{grant.execution_id}")
                if heartbeat_thread.is_alive():
                    self.journal.fence(grant.execution_id, grant.epoch, "lost")
                    checkpoint.update(recovery_pending=True, provider_thread_id=provider_thread_id)
                    self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
                    raise RuntimeError("heartbeat did not respect transport timeout")
            if cleanup_failed:
                self.journal.fence(grant.execution_id, grant.epoch, "lost")
                checkpoint.update(recovery_pending=True, provider_thread_id=provider_thread_id)
                self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
                raise RuntimeError("sandbox stop is unconfirmed; workspace remains fenced for recovery")
            self.journal.fence(grant.execution_id, grant.epoch, status)
            terminal = Operation.create(grant.execution_id, grant.epoch, "execution_finished",
                {"status": status, "reason": reason, "provider_thread_id": provider_thread_id,
                 **({"intent_reconciliation": intent_reconciliation} if intent_reconciliation is not None else {}),
                 **({"failure": failure} if failure and status == "failed" else {})})
            checkpoint.update(recovery_pending=True, pid=None, process_identity=None,
                              provider_thread_id=provider_thread_id, terminal_operation=terminal.as_dict(),
                              recovery_retry_at=0)
            if not preservation_ok:
                self._defer_recovery(checkpoint)
            self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
            self.journal.enqueue(terminal)
            if preservation_ok:
                checkpoint["recovery_pending"] = False
                self.journal.checkpoint(grant.execution_id, grant.epoch, checkpoint)
        self._flush()
        return status

    def serve(self, stop: threading.Event, *, poll_seconds: float = 5, slots: int = 1) -> None:
        if slots < 1 or poll_seconds <= 0:
            raise ValueError("positive worker slots and poll interval required")
        lock_path = self.journal.state_root / "daemon.lock"
        with lock_path.open("a") as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another daemon owns this worker journal") from error
            self.recover()
            self._retire_progress(threading.current_thread().name)
            failures: list[BaseException] = []

            def lane() -> None:
                try:
                    while not stop.is_set():
                        try:
                            self.run_once(cancel=stop)
                        except httpx.HTTPError:
                            pass
                        except (JournalFull, sqlite3.Error):
                            raise
                        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                            logging.exception("Worker lane operation failed; retaining recovery state")
                        # Keep admission-blocked diagnostics visible while idle.
                        component = self._progress_components.get(threading.current_thread().name, {})
                        self._progress(component.get("phase", "idle") if component.get("phase") in
                                       {"diagnostic_storage_pressure", "storage_pressure"} else "idle",
                                       deadline_seconds=max(120, poll_seconds * 3), details=component.get("details"))
                        if stop.wait(poll_seconds):
                            return
                except BaseException as error:
                    failures.append(error)
                    stop.set()

            def publisher() -> None:
                try:
                    while not stop.is_set():
                        self._progress("publication", deadline_seconds=1800)
                        try:
                            disposition = self.preserve_one()
                            self._flush()
                        except (JournalFull, sqlite3.Error):
                            raise
                        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, httpx.HTTPError):
                            logging.exception("Publisher operation failed; retaining durable job")
                            disposition = None
                        self._progress("idle", deadline_seconds=max(120, self.publication_poll_seconds * 3))
                        # Drain a backlog without waiting a full poll interval,
                        # but back off whenever no publication was ready.
                        if stop.wait(0.05 if disposition is not None else self.publication_poll_seconds):
                            return
                except BaseException as error:
                    failures.append(error)
                    stop.set()

            def host_health() -> None:
                try:
                    while not stop.is_set():
                        self._progress("host_heartbeat", deadline_seconds=90)
                        required = self.supervisor.max_log_bytes * 2
                        try:
                            storage = self.storage_health()
                        except (OSError, ValueError) as error:
                            # An unreadable root is unsafe for admission. Keep
                            # reporting the host so the operator can repair it.
                            storage = {"status": "storage_pressure",
                                       "cleanup_target_percent": self.cleanup_target_free_percent,
                                       "cleanup_target_bytes": 0,
                                       "cleanup_recommended": True,
                                       "roots": [], "free_bytes": 0,
                                       "required_free_bytes": self.journal.minimum_free_bytes,
                                       "error": str(error)}
                        diagnostic_ready = self.journal.diagnostic_capacity(required)
                        health = {"status": ("ready" if storage["status"] == "ready" and diagnostic_ready
                                              else "storage_pressure"),
                                  "free_bytes": storage["free_bytes"],
                                  # Container mounts currently expose only the
                                  # checkout, not linked Git metadata on the host.
                                  "capabilities": ({"workspace_preparation": 1}
                                      if all(config.sandbox is None for config in self.harnesses.values()) else {}),
                                  "required_free_bytes": max(storage["required_free_bytes"],
                                                             self.journal.minimum_free_bytes + required),
                                  "storage": storage}
                        try:
                            if self.milestone_checks:
                                health['capabilities']['milestone_verification'] = 1
                            self.transport.host_heartbeat(self.host_id, health)
                        except httpx.HTTPError:
                            pass
                        if stop.wait(30):
                            return
                except BaseException as error:
                    failures.append(error)
                    stop.set()

            self._publisher_running = True
            lanes = [threading.Thread(target=lane, name=f"horizon-worker-{index}") for index in range(slots)]
            publishers = [threading.Thread(target=publisher, name=f"horizon-publisher-{index}")
                          for index in range(self.publication_concurrency)]
            health_thread = threading.Thread(target=host_health, name="horizon-host-health")
            checkers = []
            if self.milestone_checks:
                from .milestone_jobs import serve
                checkers.append(threading.Thread(target=serve, args=(self, stop), name='horizon-milestone-checker'))
            try:
                for worker in [*lanes, *publishers, health_thread, *checkers]:
                    worker.start()
                for worker in [*lanes, *publishers, health_thread, *checkers]:
                    worker.join()
            finally:
                self._publisher_running = False
            if failures:
                raise failures[0]
