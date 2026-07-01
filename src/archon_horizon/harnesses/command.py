"""Subprocess-backed harness — how a real CLI engine plugs in.

One class drives any headless CLI (``claude -p``, ``codex exec``, ...).
The engine-specific details — binary, model, flags — are baked into
``argv_template`` when the harness is built from its ``harnesses.<name>``
config block; the engine-specific *log format* is handled by an injected
``parser`` (see :mod:`archon_horizon.transcript`). Adding an engine is a new
argv + parser, not a new code path here.

stdout is streamed line-by-line through the parser into a canonical
``transcript.jsonl`` in the request's ``artifact_dir`` as it happens, so a
live viewer can tail it; the same file is what the static dashboard reads.
A reader thread keeps cancellation and timeout responsive even while the
engine is mid-line.
"""

from __future__ import annotations

import functools
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.parsers import TranscriptParser, aggregate, observed_model, parse_plain_line
from archon_horizon.transcript.sink import JsonlTranscriptSink, NullTranscriptSink, TranscriptSink
from archon_horizon.transcript.subagents import materialize_subagent_sessions

from .base import Harness, HarnessCapability, HarnessRequest, HarnessResult

PROMPT_TOKEN = "{prompt}"
_POLL_S = 0.2

# Failure classes recognized from an engine's error output. Only the transient
# ones are retried with backoff; a usage/billing limit is a hard stop within this
# run (retrying in-process can't clear a billing window), so we label it and stop.
# Ordered so a usage/billing limit wins over a bare rate-limit match.
_FAILURE_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    ("usage_limit", re.compile(r"usage limit|insufficient[_ ]quota|quota exceeded|credit balance|billing|payment required", re.I)),
    ("rate_limit", re.compile(r"rate[ _-]?limit|too many requests|\b429\b|resource[_ ]?exhausted", re.I)),
    ("overloaded", re.compile(r"overloaded|\b529\b", re.I)),
    ("server_error", re.compile(r"internal server error|service unavailable|bad gateway|gateway timeout|\b50[0234]\b|server_error", re.I)),
    ("network", re.compile(r"connection reset|connection error|econnreset|etimedout|network error|temporarily unavailable", re.I)),
)
_RETRYABLE_REASONS = frozenset({"rate_limit", "overloaded", "server_error", "network"})


def _classify_failure(text: str) -> str | None:
    """Best-effort category for an engine failure's output, or ``None`` when it
    doesn't look like a known API/transport error."""
    for reason, pattern in _FAILURE_PATTERNS:
        if pattern.search(text):
            return reason
    return None


def _interruptible_sleep(seconds: float, cancel) -> bool:
    """Sleep in small slices so a cancel during retry backoff is honored quickly.
    Returns ``False`` if cancelled mid-sleep, ``True`` if it slept the full time."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if cancel is not None and cancel.is_cancelled():
            return False
        time.sleep(min(_POLL_S, remaining))

# Pulls an engine's native session/thread id out of one raw stream line.
SessionIdExtractor = Callable[[str], "str | None"]
# Builds the extra argv that continues a native session from its id.
ResumeArgs = Callable[[str], Sequence[str]]


def _refs(ref: str | None) -> tuple[str, ...]:
    return (ref,) if ref else ()


def _reap_process_group(
    proc: subprocess.Popen, threads: list[threading.Thread], *, grace_s: float = 2.0
) -> None:
    """Reap the engine's process group and reclaim its pipe FDs.

    The engine runs in its own session/group (``start_new_session=True``), so the
    group id equals the engine pid. Signal the whole group — SIGTERM, then
    SIGKILL if needed — to clear any grandchildren (MCP servers, native
    subagents) that inherited and still hold the stdout/stderr pipe open. Once
    they exit the reader threads hit EOF; join them, then close the pipes so no
    descriptor leaks. Without this, a held pipe blocks a reader thread forever,
    pinning the parent's FDs until the system table is exhausted (ENFILE).
    """
    pgid = proc.pid  # == process-group id thanks to start_new_session=True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break  # group already gone, or not signallable (non-POSIX)
        for thread in threads:
            thread.join(timeout=grace_s)
        if not any(thread.is_alive() for thread in threads):
            break
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


@functools.lru_cache(maxsize=1)
def _horizon_bin() -> str | None:
    """Absolute path to the ``horizon`` CLI, so agents can call it even when their
    shell doesn't inherit the venv's PATH. Falls back to the script sitting next
    to the running interpreter."""
    found = shutil.which("horizon")
    if found:
        return found
    candidate = Path(sys.executable).with_name("horizon")
    return str(candidate) if candidate.exists() else None


class CommandHarness(Harness):
    capabilities = frozenset({HarnessCapability.CANCELLATION, HarnessCapability.STREAMING})

    # Whether stdout-parsed events are written to the transcript sink. Engines
    # whose rich transcript lives in a FILE set this False and emit the file's
    # events from ``_after_stream`` instead, keeping stdout only as the source of
    # the result text.
    _sink_stdout = True

    def __init__(
        self,
        name: str,
        argv_template: Sequence[str],
        *,
        parser: TranscriptParser = parse_plain_line,
        env_overrides: dict[str, str] | None = None,
        session_id_of: "SessionIdExtractor | None" = None,
        resume_args: "ResumeArgs | None" = None,
    ) -> None:
        self.name = name
        self._argv_template = tuple(argv_template)
        self._parser = parser
        self.env_overrides = dict(env_overrides) if env_overrides else {}
        # Extracts the engine's native session/thread id from a raw stream line,
        # so it can be stamped into the session JSON for a later native --resume.
        self._session_id_of = session_id_of
        # Builds the argv that continues a native session (e.g. ``--resume <id>``).
        # When present the harness advertises RESUME; engines without it (codex)
        # leave it None and a resume falls back to a fresh, same-prompt run.
        self._resume_args = resume_args
        if resume_args is not None:
            self.capabilities = self.capabilities | {HarnessCapability.RESUME}

    def _argv(self, prompt: str, extra: Sequence[str] = ()) -> list[str]:
        extra = list(extra)
        if PROMPT_TOKEN in self._argv_template:
            out: list[str] = []
            for tok in self._argv_template:
                if tok == PROMPT_TOKEN:
                    out.extend(extra)
                    out.append(prompt)
                else:
                    out.append(tok)
            return out
        return [*self._argv_template, *extra, prompt]

    def _extra_argv(self, request: HarnessRequest) -> list[str]:
        """Per-run argv flags an engine needs (e.g. a ``--log-file``)."""
        return []

    def _after_stream(self, request: HarnessRequest, sink: TranscriptSink) -> None:
        """Hook after stdout streaming ends — e.g. ingest a file-based transcript."""
        return None

    # Transient API failures (rate limit, overload, 5xx, network) are retried with
    # exponential backoff. A usage/billing limit is a hard stop (retrying can't
    # clear it) and is labelled instead. Overridable per harness via
    # ``options.max_retries`` / ``options.retry_base_seconds``.
    retry_max = 2
    retry_base_seconds = 8.0

    def run(self, request: HarnessRequest) -> HarnessResult:
        ref: str | None = None
        sink: TranscriptSink = NullTranscriptSink()
        if request.artifact_dir is not None:
            path = request.artifact_dir / "transcript.jsonl"
            sink = JsonlTranscriptSink(path)
            ref = path.as_posix()
        # Carry the prompt on session_start so the dashboard can show the exact
        # input the agent received as the first event.
        sink.emit(TranscriptEvent(
            TranscriptKind.SESSION_START, data={"harness": self.name, "prompt": request.prompt}
        ))

        if request.cancel is not None and request.cancel.is_cancelled():
            sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": False, "reason": "cancelled"}))
            return HarnessResult(ok=False, text="cancelled before start", artifact_refs=_refs(ref))

        # Retry loop: SESSION_START/END bracket the whole thing; each attempt
        # appends its events, with a marker between attempts on a transient error.
        attempt = 0
        while True:
            result, reason = self._attempt(request, sink, ref)
            attempt += 1
            if result.ok or reason not in _RETRYABLE_REASONS or attempt > self.retry_max:
                break
            if request.cancel is not None and request.cancel.is_cancelled():
                break
            delay = self.retry_base_seconds * (2 ** (attempt - 1))
            sink.emit(TranscriptEvent(
                TranscriptKind.ERROR,
                text=f"{self.name}: transient API error ({reason}); retrying in {delay:.0f}s "
                     f"(attempt {attempt + 1}/{self.retry_max + 1}).",
            ))
            if not _interruptible_sleep(delay, request.cancel):
                break  # cancelled during backoff

        end_data: dict[str, object] = {"ok": result.ok, "returncode": result.metadata.get("returncode")}
        if result.metadata.get("model"):
            end_data["model"] = result.metadata["model"]
        if result.metadata.get("session_id"):
            end_data["session_id"] = result.metadata["session_id"]
        if reason:
            # Label the failure so the run view/status can tell a rate/usage limit
            # apart from a genuine task failure (and note if retries ran out).
            end_data["failure_reason"] = reason
            result.metadata["failure_reason"] = reason
            if reason in _RETRYABLE_REASONS and attempt > self.retry_max:
                end_data["retries_exhausted"] = True
        sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data=end_data))
        return result

    def _attempt(self, request: HarnessRequest, sink: TranscriptSink, ref: str | None) -> tuple[HarnessResult, str | None]:
        """One engine invocation. Returns the result and a failure classification
        (``None`` when ok or the failure is unrecognized). Does not emit
        SESSION_START/END — ``run`` brackets the (possibly retried) whole."""
        try:
            env = dict(os.environ)
            if self.env_overrides:
                env.update(self.env_overrides)
            # Make the `horizon` CLI reachable from the agent's shell. Some engines
            # (codex) wrap commands in a login shell that resets PATH and drops the
            # venv, so a bare `horizon inbox …` fails. Export an absolute HORIZON_BIN
            # (which a login shell preserves) and also prepend its dir to PATH.
            hb = _horizon_bin()
            if hb:
                env.setdefault("HORIZON_BIN", hb)
                env["PATH"] = os.path.dirname(hb) + os.pathsep + env.get("PATH", "")
            request_env = request.metadata.get("env") if isinstance(request.metadata, dict) else None
            if isinstance(request_env, dict):
                env.update({str(k): str(v) for k, v in request_env.items()})
            extra = list(self._extra_argv(request))
            if request.resume_session_id and self._resume_args is not None:
                extra = [*self._resume_args(request.resume_session_id), *extra]
            proc = subprocess.Popen(
                self._argv(request.prompt, extra),
                cwd=request.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                # Own process group, so cleanup can reap the engine AND any
                # grandchildren it spawned (MCP servers, native subagents). They
                # inherit the stdout/stderr pipe; if they outlive the engine they
                # hold the write end open, the reader threads block forever, and
                # the parent's pipe FDs leak until the system runs out (ENFILE).
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            sink.emit(TranscriptEvent(TranscriptKind.ERROR, text=str(exc)))
            return HarnessResult(ok=False, text=f"{self.name}: engine not found: {exc}", artifact_refs=_refs(ref)), None

        events, stderr_lines, engine_session_id, threads = self._stream(proc, request, sink)

        timed_out = events is None
        cancelled = request.cancel is not None and request.cancel.is_cancelled()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        returncode = proc.returncode if proc.returncode is not None else -1
        # Reap the whole process group and reclaim the pipe FDs. Done after the
        # real returncode is captured, so killing stragglers can't mask the
        # engine's own exit status.
        _reap_process_group(proc, threads)
        ok = returncode == 0 and not timed_out and not cancelled

        text, usage = aggregate(events or [])
        stderr_text = "".join(stderr_lines).strip()
        if not ok and not text:
            text = stderr_text or (
                "timed out" if timed_out else "cancelled" if cancelled else f"exit {returncode}"
            )
        try:
            self._after_stream(request, sink)
        except Exception as exc:  # file ingestion must never crash the run
            sink.emit(TranscriptEvent(TranscriptKind.ERROR, text=f"{self.name}: transcript ingest failed: {exc}"))
        try:
            if request.artifact_dir is not None:
                materialize_subagent_sessions(request.artifact_dir)
        except Exception as exc:  # derived child logs must never crash the run
            sink.emit(TranscriptEvent(
                TranscriptKind.ERROR,
                text=f"{self.name}: subagent log materialization failed: {exc}",
            ))
        result_meta: dict[str, object] = {"returncode": returncode, "timed_out": timed_out, "cancelled": cancelled}
        # The model the engine actually used, read from its own stream — present
        # even when the config never pinned one. Recorded so the run view shows it.
        # Fall back to the configured model when the engine's stream doesn't
        # announce one (codex's `exec --json` stdout carries no model field — only
        # its separate rollout does — so without this the run view showed a blank).
        model = observed_model(events or []) or getattr(self, "horizon_model", None)
        if model:
            result_meta["model"] = model
        if engine_session_id:
            # Stamp the engine's session id into the result, which the orchestrator
            # records in the session meta for a later native --resume.
            result_meta["session_id"] = engine_session_id
        # Classify a failure from the engine's own output (never our synthetic
        # cancel/timeout text) so `run` can retry transient ones and label the rest.
        reason = _classify_failure(f"{stderr_text}\n{text}") if (not ok and not cancelled) else None
        return HarnessResult(
            ok=ok,
            text=text,
            usage=usage,
            artifact_refs=_refs(ref),
            metadata=result_meta,
        ), reason

    def _stream(
        self, proc: subprocess.Popen, request: HarnessRequest, sink: TranscriptSink
    ) -> tuple[list[TranscriptEvent] | None, list[str], str | None, list[threading.Thread]]:
        """Pump stdout through the parser. Returns (events, stderr, session_id,
        reader_threads); events is None if the run timed out (so the caller can
        flag it), session_id is the engine's native id once seen (None
        otherwise), and the reader threads are handed back so the caller can join
        them after it has reaped the process group."""
        out_q: queue.Queue[str | None] = queue.Queue()
        stderr_lines: list[str] = []
        session_id: str | None = None

        def pump_stdout() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    out_q.put(line)
            finally:
                out_q.put(None)

        def drain_stderr() -> None:
            if proc.stderr is not None:
                try:
                    stderr_lines.extend(proc.stderr)
                except (ValueError, OSError):
                    pass  # pipe closed under us during cleanup

        threads = [
            threading.Thread(target=pump_stdout, daemon=True),
            threading.Thread(target=drain_stderr, daemon=True),
        ]
        for thread in threads:
            thread.start()

        events: list[TranscriptEvent] = []
        deadline = time.monotonic() + request.timeout_s if request.timeout_s else None
        while True:
            if request.cancel is not None and request.cancel.is_cancelled():
                proc.terminate()
                return events, stderr_lines, session_id, threads
            if deadline is not None and time.monotonic() > deadline:
                proc.terminate()
                return None, stderr_lines, session_id, threads
            try:
                line = out_q.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            if line is None:
                return events, stderr_lines, session_id, threads
            stripped = line.rstrip("\n")
            if session_id is None and self._session_id_of is not None:
                session_id = self._session_id_of(stripped)
                if session_id:
                    # Emit it live so a session that crashes mid-run is still
                    # resumable from its partial transcript (Archon's session_meta).
                    sink.emit(TranscriptEvent(TranscriptKind.SESSION_META, data={"session_id": session_id}))
            for event in self._parser(stripped):
                if self._sink_stdout:
                    sink.emit(event)
                events.append(event)
