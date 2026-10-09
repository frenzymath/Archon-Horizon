"""Run native coding providers within the pinned execution policy.

Preserve their tools, customization, collaboration and context management. The
adapter adds unattended permission behavior and supervised process ownership.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from .contracts import FencedExecution, require_identifier, validate_subagent_limit
from .journal import DurableJournal, boot_identity


def process_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19]
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return None


class PhysicalStopUnconfirmed(RuntimeError):
    pass


def _stop_owned_session(session_id: int, *, identity: str, timeout: float = 5) -> bool:
    """Stop every group in the provider's session, including managed build groups.

    This is lifecycle fencing, not a sandbox against deliberate setsid escapes.
    Rootless containers enforce that stronger process boundary when configured.
    """
    deadline = time.monotonic() + timeout
    signalled = False
    while True:
        current = process_identity(session_id)
        if current is not None and current != identity:
            return signalled
        groups = set()
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            try:
                fields = (path / "stat").read_text().rsplit(")", 1)[1].split()
                if int(fields[3]) == session_id and fields[0] not in {"Z", "X"}:
                    groups.add(int(fields[2]))
            except (FileNotFoundError, ProcessLookupError):
                continue
        if not groups:
            return signalled
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
                signalled = True
            except ProcessLookupError:
                pass
        if time.monotonic() >= deadline:
            raise PhysicalStopUnconfirmed("Owned provider session has not stopped")
        time.sleep(0.05)


def terminate_owned_process(checkpoint: dict[str, Any]) -> bool:
    """Never signal a reused PID or a process from another boot."""
    pid = checkpoint.get("pid")
    identity = checkpoint.get("process_identity")
    if (not isinstance(pid, int) or pid <= 1 or not identity
            or checkpoint.get("boot_id") != boot_identity()):
        return False
    current = process_identity(pid)
    if current is not None and current != identity:
        return False
    try:
        if current is not None and os.getsid(pid) != pid:
            raise PhysicalStopUnconfirmed("Recorded provider process changed its session")
    except ProcessLookupError:
        pass
    # A dead session leader can leave living groups; Linux retains its session
    # identifier while they exist, so a mismatching live PID is never signalled.
    return _stop_owned_session(pid, identity=identity)


@dataclass(frozen=True)
class HeadlessAdapter:
    provider: str
    executable: str
    model: str | None = None
    reasoning_effort: str | None = None
    approval_mode: str = "deny"
    sandbox_mode: str = "workspace_write"
    tool_names: tuple[str, ...] = ()
    auto_compaction: bool = True
    max_parallel_subagents: int | None = None
    # Collaboration and current-source research belong in ordinary worker runs.
    # Pinned harness settings can opt into an older mode or narrower discovery.
    codex_multi_agent_v2: bool = True
    codex_web_search: str = "live"
    claude_native_configuration: bool = True

    def validate(self, *, externally_isolated: bool) -> None:
        if self.approval_mode not in {"deny", "preauthorized"}:
            raise ValueError("automatic approval review is not supported by this headless adapter")
        if self.sandbox_mode not in {"read_only", "workspace_write", "externally_isolated"}:
            raise ValueError("unsupported provider sandbox policy")
        if (self.approval_mode == "preauthorized" or self.sandbox_mode == "externally_isolated") and not externally_isolated:
            raise ValueError("preauthorized/external provider execution requires local Podman isolation")
        if self.approval_mode == "preauthorized" and self.sandbox_mode != "externally_isolated":
            raise ValueError("preauthorized execution requires the externally_isolated provider policy")
        if self.auto_compaction is not True:
            raise ValueError("disabling native automatic compaction is not supported")
        validate_subagent_limit(self.max_parallel_subagents)
        if type(self.codex_multi_agent_v2) is not bool or type(self.claude_native_configuration) is not bool:
            raise ValueError("native feature settings must be booleans")
        if not isinstance(self.codex_web_search, str) or self.codex_web_search not in {"live", "cached", "disabled"}:
            raise ValueError("unsupported Codex web-search mode")
        if self.provider == "codex_exec":
            if self.tool_names:
                raise ValueError("Codex headless tool-name allowlists are not supported")
        elif self.provider == "claude_exec":
            if self.sandbox_mode == "workspace_write":
                raise ValueError("Claude workspace_write is unsupported; use rootless externally_isolated or read_only")
            if self.max_parallel_subagents is not None and self.max_parallel_subagents > 0:
                raise ValueError("Claude native parallel-subagent bounds cannot be enforced by this CLI adapter; use null or zero")
            # The provider owns its evolving built-in tool catalog. Validate
            # names without freezing writable runs to an old list of tools.
            if any(not isinstance(tool, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", tool)
                   for tool in self.tool_names):
                raise ValueError("unsupported Claude tool name")
            if self.sandbox_mode == "read_only" and not set(self.tool_names).issubset({"Read", "Glob", "Grep"}):
                raise ValueError("unsupported Claude tool in the configured sandbox mode")
            if self.max_parallel_subagents == 0 and {"Agent", "Task"}.intersection(self.tool_names):
                raise ValueError("unsupported Claude tool in the configured sandbox mode")
        else:
            raise ValueError("unsupported provider adapter")

    def command(self, *, provider_thread_id: str | None = None,
                externally_isolated: bool = False,
                agent_state_path: Path | None = None,
                tool_writable_roots: tuple[Path, ...] = ()) -> list[str]:
        self.validate(externally_isolated=externally_isolated)
        if tool_writable_roots:
            if self.provider != "codex_exec" or self.sandbox_mode != "workspace_write" or externally_isolated:
                raise ValueError("tool writable roots require host Codex workspace_write")
            if any(not root.is_absolute() or root == Path("/") or ".." in root.parts for root in tool_writable_roots):
                raise ValueError("tool writable roots must be explicit absolute directories")
        if agent_state_path is not None:
            if self.provider != "codex_exec" or self.sandbox_mode != "workspace_write" or externally_isolated:
                raise ValueError("agent intent writable root is only supported for host Codex workspace_write")
            if not agent_state_path.is_absolute() or agent_state_path == Path("/") or ".." in agent_state_path.parts:
                raise ValueError("agent intent writable root must be an explicit absolute directory")
        if provider_thread_id:
            require_identifier(provider_thread_id)
        if self.provider == "codex_exec":
            args = [self.executable, "exec"]
            if provider_thread_id:
                args.extend(["resume", provider_thread_id])
            args.extend(["--json", "--skip-git-repo-check", "-c", 'approval_policy="never"'])
            mode = {"read_only": "read-only", "workspace_write": "workspace-write", "externally_isolated": "danger-full-access"}[self.sandbox_mode]
            args.extend(["-c", "sandbox_mode=" + json.dumps(mode)])
            if self.sandbox_mode == "workspace_write":
                args.extend(["-c", "sandbox_workspace_write.network_access=true"])
                roots = ([str(agent_state_path)] if agent_state_path is not None else []) + [str(root) for root in tool_writable_roots]
                if roots:
                    args.extend(["-c", "sandbox_workspace_write.writable_roots=" + json.dumps(roots)])
            enabled = self.max_parallel_subagents != 0
            args.extend(["-c", "agents.enabled=" + ("true" if enabled else "false")])
            args.extend(["-c", "features.multi_agent=" + ("true" if enabled else "false"),
                         "-c", "features.multi_agent_v2=" + ("true" if enabled and self.codex_multi_agent_v2 else "false"),
                         "-c", "web_search=" + json.dumps(self.codex_web_search)])
            if self.max_parallel_subagents:
                args.extend(["-c", "agents.max_threads=" + str(self.max_parallel_subagents)])
            if self.approval_mode == "preauthorized":
                args.append("--dangerously-bypass-approvals-and-sandbox")
            if self.model:
                args.extend(["--model", self.model])
            if self.reasoning_effort:
                args.extend(["-c", "model_reasoning_effort=" + json.dumps(self.reasoning_effort)])
            return [*args, "-"]
        if self.provider == "claude_exec":
            args = [self.executable, "--print", "--verbose", "--output-format", "stream-json",
                    "--permission-mode", "bypassPermissions" if self.approval_mode == "preauthorized" else "dontAsk",
                    "--permission-prompts", "none", "--autocompact", "auto"]
            if not self.claude_native_configuration or self.sandbox_mode == "read_only":
                # Native hooks and MCP commands can execute outside the tool
                # allowlist; discovery remains restricted for host read-only runs.
                args.extend(["--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}'])
            # Keep the provider’s default tools as they evolve. Disabling native
            # delegation removes just Agent/Task, preserving unrelated tools.
            tools = ",".join(self.tool_names) if self.tool_names else (
                "Read,Glob,Grep" if self.sandbox_mode == "read_only" else
                "default")
            args.extend(["--tools", tools])
            if self.max_parallel_subagents == 0:
                args.extend(["--disallowedTools", "Agent,Task"])
            if provider_thread_id:
                args.extend(["--resume", provider_thread_id])
            if self.model:
                args.extend(["--model", self.model])
            if self.reasoning_effort:
                args.extend(["--effort", self.reasoning_effort])
            return args
        raise ValueError("unsupported provider adapter")

    def runtime_environment(self) -> dict[str, str]:
        """Let helpers finish within Horizon's existing execution deadline.

        Claude print mode otherwise abandons background helpers after ten idle
        minutes. Horizon already renews/fences the lease and enforces wall time.
        """
        return {"CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "0"} if self.provider == "claude_exec" else {}


@dataclass(frozen=True)
class ProcessResult:
    status: str
    returncode: int
    provider_thread_id: str | None
    stdout_path: str
    stderr_path: str
    omitted_bytes: int
    reason: str | None = None
    failure: dict[str, str] | None = None


def provider_failure(event: dict[str, Any]) -> dict[str, str]:
    error = event.get("error")
    details = error if isinstance(error, dict) else {}
    code = details.get("code") or details.get("type") or event.get("error_code") or event.get("api_error")
    status = details.get("status_code", details.get("status", event.get("status_code")))
    categories = {
        "rate_limit_exceeded": ("provider", "rate_limited"), "rate_limit_error": ("provider", "rate_limited"),
        "rate_limited": ("provider", "rate_limited"), "overloaded_error": ("provider", "provider_overloaded"),
        "server_error": ("provider", "provider_overloaded"), "authentication_error": ("configuration", "authentication_failed"),
        "invalid_api_key": ("configuration", "authentication_failed"), "permission_error": ("configuration", "authentication_failed"),
        "connection_error": ("transport", "connection_error"), "timeout": ("transport", "timeout"),
        "request_timeout": ("transport", "timeout"),
    }
    selected = categories.get(code) if isinstance(code, str) else None
    if selected is None:
        selected = ("provider", "rate_limited") if status in (429, "429") else (
            ("configuration", "authentication_failed") if status in (401, 403, "401", "403") else (
                ("provider", "provider_overloaded") if status in (500, 502, 503, 504, 529, "500", "502", "503", "504", "529")
                else ("execution", "execution_failed")))
    kind, code = selected
    return {"kind": kind, "code": code, "message": "Provider request failed: " + code}


class ProcessSupervisor:
    def __init__(self, journal: DurableJournal, *, max_log_bytes: int = 4 * 1024 * 1024,
                 max_request_seconds: float = 86400, poll_seconds: float = 0.1) -> None:
        if max_log_bytes < 1024 or max_request_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("invalid process supervision bounds")
        self.journal = journal
        self.max_log_bytes = max_log_bytes
        self.max_request_seconds = max_request_seconds
        self.poll_seconds = poll_seconds

    def run(self, command: list[str], *, prompt: str, request_id: str,
            execution_id: str, epoch: int, workspace: Path, env: dict[str, str],
            provider_thread_id: str | None = None,
            cancel: threading.Event | None = None,
            checkpoint: dict[str, Any] | None = None,
            on_event: Callable[[dict[str, Any]], None] | None = None,
            lifecycle_observer=None) -> ProcessResult:
        require_identifier(request_id)
        if not command or not workspace.is_absolute():
            raise ValueError("command and absolute workspace required")
        self.journal.assert_lease(execution_id, epoch)
        if self.journal.request(request_id) is None:
            self.journal.reserve_diagnostics(request_id, execution_id, epoch, self.max_log_bytes * 2)
        if not self.journal.begin_request(request_id, execution_id, epoch, prompt):
            previous = self.journal.request(request_id)
            if previous and previous["state"] in {"completed", "failed", "interrupted"} and previous["result"]:
                return ProcessResult(**json.loads(previous["result"]))
            raise FencedExecution("provider submission is uncertain; reconcile before continuing")
        directory = self.journal.state_root / "requests" / request_id
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        stdout_path, stderr_path = directory / "stdout.jsonl", directory / "stderr.log"
        saved = dict(checkpoint or {})
        saved.update(request_id=request_id, provider_thread_id=provider_thread_id,
                     workspace_path=str(workspace), boot_id=boot_identity())
        self.journal.checkpoint(execution_id, epoch, saved)
        fd, input_path = tempfile.mkstemp(prefix="input-", dir=directory)
        with os.fdopen(fd, "wb") as stream:
            stream.write(prompt.encode())
            stream.flush()
            os.fsync(stream.fileno())
        process: subprocess.Popen[bytes] | None = None
        selector = selectors.DefaultSelector()
        reason: str | None = None
        failure: dict[str, str] | None = None
        status = "succeeded"
        omitted = 0
        pending = b""
        started = time.monotonic()
        files: dict[str, Any] = {}
        gate_read, gate_write = os.pipe()
        try:
            with open(input_path, "rb") as input_stream:
                self.journal.assert_lease(execution_id, epoch)
                # Provider tools cannot start before their PID identity is durable.
                bootstrap = "import os,sys; fd=int(sys.argv[1]); ok=os.read(fd,1); os.close(fd); " \
                            "sys.exit(125) if ok!=b'1' else os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
                process = subprocess.Popen([sys.executable, "-c", bootstrap, str(gate_read), *command],
                                           cwd=workspace, env=env, stdin=input_stream,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True, pass_fds=(gate_read,))
            os.close(gate_read)
            gate_read = -1
            saved.update(pid=process.pid, process_identity=process_identity(process.pid))
            self.journal.checkpoint(execution_id, epoch, saved)
            os.write(gate_write, b"1")
            os.close(gate_write)
            gate_write = -1
            for label, pipe, path in (("stdout", process.stdout, stdout_path), ("stderr", process.stderr, stderr_path)):
                assert pipe is not None
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, label)
                files[label] = path.open("wb")
                os.chmod(path, 0o600)
            sizes = {"stdout": 0, "stderr": 0}
            exited_at: float | None = None
            while selector.get_map() or process.poll() is None:
                try:
                    self.journal.assert_lease(execution_id, epoch)
                    if cancel is not None and cancel.is_set():
                        status, reason = "cancelled", "cancel_requested"
                    elif time.monotonic() - started >= self.max_request_seconds:
                        status, reason = "failed", "request_deadline"
                except FencedExecution:
                    status, reason = "lost", "lease_expired"
                if reason:
                    _stop_owned_session(process.pid, identity=saved["process_identity"])
                for key, _ in selector.select(self.poll_seconds):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    label = key.data
                    available = max(0, self.max_log_bytes - sizes[label])
                    files[label].write(chunk[:available])
                    sizes[label] += min(len(chunk), available)
                    omitted += max(0, len(chunk) - available)
                    if label == "stdout":
                        pending += chunk
                        while b"\n" in pending:
                            line, pending = pending.split(b"\n", 1)
                            if len(line) > 1024 * 1024:
                                continue
                            try:
                                event = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            if not isinstance(event, dict):
                                continue
                            native_id = event.get("thread_id") or event.get("session_id")
                            if isinstance(native_id, str) and native_id:
                                require_identifier(native_id)
                                provider_thread_id = native_id
                                saved["provider_thread_id"] = native_id
                                self.journal.checkpoint(execution_id, epoch, saved)
                            if event.get("type") in {"thread.started", "turn.completed", "turn.failed", "result", "system"}:
                                if event.get("type") == "turn.failed" or event.get("is_error") is True:
                                    status, reason = "failed", "provider_failed"
                                    failure = provider_failure(event)
                            elif event.get("type") == "error":
                                failure = provider_failure(event)
                            if on_event:
                                if lifecycle_observer is not None:
                                    event = lifecycle_observer.annotate(event, provider_thread_id)
                                on_event(event)
                        if len(pending) > 1024 * 1024:
                            pending = b""
                if lifecycle_observer is not None:
                    lifecycle_observer.poll(provider_thread_id)
                if process.poll() is not None:
                    exited_at = exited_at or time.monotonic()
                    if time.monotonic() - exited_at > 1:
                        _stop_owned_session(process.pid, identity=saved["process_identity"])
                        break
            code = process.wait(timeout=5)
            if lifecycle_observer is not None:
                lifecycle_observer.poll(provider_thread_id, force=True)
            # A successful parent can leave tools running with redirected pipes.
            # The workspace cannot be released until the owned group has stopped.
            _stop_owned_session(process.pid, identity=saved["process_identity"])
            if code != 0 and status == "succeeded":
                status, reason = "failed", "provider_exit"
        except BaseException:
            if process is not None:
                _stop_owned_session(process.pid, identity=saved["process_identity"])
                process.wait(timeout=5)
            # No acknowledgement of provider completion: keep request uncertain.
            raise
        finally:
            for gate in (gate_read, gate_write):
                if gate >= 0:
                    os.close(gate)
            selector.close()
            if process is not None:
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            for stream in files.values():
                stream.flush()
                os.fsync(stream.fileno())
                stream.close()
            Path(input_path).unlink(missing_ok=True)
        if reason == "request_deadline":
            failure = {"kind": "execution", "code": "request_deadline", "message": "Provider request exceeded its execution deadline"}
        if status != "failed":
            failure = None
        result = ProcessResult(status, code, provider_thread_id, str(stdout_path), str(stderr_path), omitted, reason, failure)
        self.journal.finish_request(request_id, state="completed" if status == "succeeded" else "interrupted" if status in {"cancelled", "lost"} else "failed",
                                    provider_thread_id=provider_thread_id, result=asdict(result))
        self.journal.release_diagnostics(request_id)
        saved.update(pid=None, process_identity=None, provider_thread_id=provider_thread_id)
        self.journal.checkpoint(execution_id, epoch, saved)
        return result
