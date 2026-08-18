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
    ("auth_error", re.compile(r"not logged in|please run /login|unauthenticated|authentication required|invalid api key|incorrect api key|unauthorized|auth error", re.I)),
    ("usage_limit", re.compile(r"usage limit|session limit|hit your .*limit|insufficient[_ ]quota|quota exceeded|credit balance|billing|payment required", re.I)),
    ("rate_limit", re.compile(r"rate[ _-]?limit|too many requests|\b429\b|resource[_ ]?exhausted", re.I)),
    ("overloaded", re.compile(r"overloaded|\b529\b", re.I)),
    ("server_error", re.compile(r"internal server error|service unavailable|bad gateway|gateway timeout|\b50[0234]\b|server[ _-]?error|error mid-?response|may be incomplete", re.I)),
    ("network", re.compile(r"connection reset|connection error|econnreset|etimedout|network error|temporarily unavailable", re.I)),
)
_RETRYABLE_REASONS = frozenset({"rate_limit", "overloaded", "server_error", "network"})

# A failed session that produced no output and exited faster than this almost
# never did real work — it's the engine refusing to start (a session/usage cap,
# a bad flag, an auth wall whose message we don't pattern-match). Treat it as a
# hard stop (``aborted_early``) so the run loop halts instead of respawning a
# session that will die the same way every round.
_EARLY_ABORT_S = 3.0


def _classify_failure(text: str) -> str | None:
    """Best-effort category for an engine failure's output, or ``None`` when it
    doesn't look like a known API/transport error."""
    for reason, pattern in _FAILURE_PATTERNS:
        if pattern.search(text):
            return reason
    return None


# "Retry after 12s" / "try again in 3 seconds" / "retry-after: 30" — engines and
# gateways often advertise the wait; honor it instead of guessing a backoff.
_RETRY_AFTER_RE = re.compile(
    r"(?:retry[- ]?after[:\s]+|try again in\s+|retry in\s+)(\d+(?:\.\d+)?)\s*(?:s\b|sec|second|$|\D)",
    re.I,
)
_MAX_ADVERTISED_RETRY_S = 15 * 60.0


def _advertised_retry_s(text: str) -> float | None:
    """The wait the engine's error text advertises, in seconds (capped), or None."""
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return None
    try:
        return min(float(match.group(1)), _MAX_ADVERTISED_RETRY_S)
    except ValueError:
        return None


class _UsageFile:
    """Live per-session usage telemetry: ``<session_dir>/usage.json``.

    Folds USAGE events into a running total as the engine streams, and rewrites
    the file (atomically) at most every couple of seconds — cheap enough for the
    agent to poll via ``horizon usage`` mid-session on any engine.
    """

    _WRITE_INTERVAL_S = 2.0

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._totals = {
            "schema_version": 3,
            "tokens_in": 0,
            "tokens_out": 0,
            "cached_tokens_in": 0,
            "reasoning_tokens_out": 0,
            "cost_usd": None,
            "usage_events": 0,
            "started_at": time.time(),
            "updated_at": None,
            "compaction_count": 0,
            "last_compaction_at": None,
            "context": {},
        }
        self._dirty = False
        self._written_at = 0.0
        self._last_cumulative_cost: float | None = None
        self._lock = threading.RLock()

    def add(self, event: TranscriptEvent) -> None:
        with self._lock:
            self._add(event)

    def _add(self, event: TranscriptEvent) -> None:
        if self._path is None:
            return
        if event.kind is TranscriptKind.COMPACTION:
            self._totals["compaction_count"] += 1
            self._totals["last_compaction_at"] = event.at.isoformat()
            self._dirty = True
            self.flush()
            return
        if event.kind is TranscriptKind.CONTEXT:
            keys = (
                "request_tokens_in", "request_tokens_out", "request_cached_tokens_in",
                "cumulative_tokens_in", "cumulative_tokens_out", "cumulative_cached_tokens_in",
                "model_context_window",
            )
            self._totals["context"] = {key: event.data.get(key, 0) for key in keys}
            self._dirty = True
            self.flush()
            return
        if event.kind is not TranscriptKind.USAGE or event.usage is None:
            return
        usage = event.usage
        # Only canonical USAGE events count. Text/tool rows may repeat the same
        # native message usage on every derived content block.
        self._totals["tokens_in"] += int(usage.tokens_in or 0)
        self._totals["tokens_out"] += int(usage.tokens_out or 0)
        self._totals["cached_tokens_in"] += int(getattr(usage, "cached_tokens_in", 0) or 0)
        self._totals["reasoning_tokens_out"] += int(getattr(usage, "reasoning_tokens_out", 0) or 0)
        cost = getattr(usage, "cost_usd", None)
        if cost is not None:
            current = float(cost)
            if event.data.get("cost_cumulative"):
                delta = (current if self._last_cumulative_cost is None
                         or current < self._last_cumulative_cost
                         else current - self._last_cumulative_cost)
                self._last_cumulative_cost = current
            else:
                delta = current
            self._totals["cost_usd"] = (self._totals["cost_usd"] or 0.0) + max(delta, 0.0)
        self._totals["usage_events"] += 1
        self._dirty = True
        self.flush()

    def flush(self, *, force: bool = False) -> None:
        with self._lock:
            self._flush(force=force)

    def _flush(self, *, force: bool = False) -> None:
        if self._path is None or not self._dirty:
            return
        now = time.monotonic()
        if not force and now - self._written_at < self._WRITE_INTERVAL_S:
            return
        self._totals["updated_at"] = time.time()
        try:
            import json as _json

            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(self._totals), "utf-8")
            os.replace(tmp, self._path)
            self._written_at = now
            self._dirty = False
        except OSError:
            pass  # telemetry must never break the run

    @property
    def tokens_out(self) -> int:
        with self._lock:
            return int(self._totals["tokens_out"])


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

    def _after_stream_line(
        self, request: HarnessRequest, sink: TranscriptSink, line: str,
    ) -> None:
        """Observe one raw engine line while the process is still running.

        File-backed engines use this to start live side-channel watchers as soon
        as the parent session id appears. The default is intentionally empty so
        ordinary command harnesses pay no work beyond the call.
        """
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
        usage_file = _UsageFile(
            request.artifact_dir / "usage.json" if request.artifact_dir is not None else None
        )
        self._usage_file = usage_file
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
            # Honor an advertised wait ("retry after 30s") over the guessed
            # backoff — waiting less just burns an attempt on the same wall.
            advertised = _advertised_retry_s(result.text or "")
            if advertised is not None:
                delay = max(delay, advertised)
            # A NOTICE, not an ERROR: this is a transient wait-and-retry, so a run
            # that is merely backing off must not be rendered as failed.
            sink.emit(TranscriptEvent(
                TranscriptKind.NOTICE,
                text=f"{self.name}: transient API error ({reason}); retrying in {delay:.0f}s "
                     f"(attempt {attempt + 1}/{self.retry_max + 1}).",
            ))
            if not _interruptible_sleep(delay, request.cancel):
                break  # cancelled during backoff

        end_data: dict[str, object] = {"ok": result.ok, "returncode": result.metadata.get("returncode")}
        if result.metadata.get("model"):
            end_data["model"] = result.metadata["model"]
        if result.metadata.get("effort"):
            end_data["effort"] = result.metadata["effort"]
        if result.metadata.get("session_id"):
            end_data["session_id"] = result.metadata["session_id"]
        if reason:
            # Label the failure so the run view/status can tell a rate/usage limit
            # apart from a genuine task failure (and note if retries ran out).
            end_data["failure_reason"] = reason
            result.metadata["failure_reason"] = reason
            retry_hint = _advertised_retry_s(result.text or "")
            if retry_hint is not None:
                # Surfaced so a pause marker / relaunch loop knows when a retry
                # is worthwhile (the supervisor itself never sleeps on it).
                end_data["retry_after_s"] = retry_hint
                result.metadata["retry_after_s"] = retry_hint
            if reason in _RETRYABLE_REASONS and attempt > self.retry_max:
                # Stamp BOTH the transcript event and the result metadata: the run
                # loop's fatal-failure check reads ``result.metadata`` (not the
                # transcript), so a rate/overload limit that survives every retry
                # must carry the flag here or the run keeps respawning sessions
                # that hit the same wall each round.
                end_data["retries_exhausted"] = True
                result.metadata["retries_exhausted"] = True
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
            started_at = time.monotonic()
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
        elapsed = time.monotonic() - started_at
        usage_file = getattr(self, "_usage_file", None)
        if usage_file is not None:
            usage_file.flush(force=True)

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
        had_output = bool(text.strip())
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
        # The reasoning-effort tier the harness ran with (Codex effort / Claude
        # thinking budget), recorded so the run view can show it next to the model.
        effort = getattr(self, "horizon_effort", None)
        if effort:
            result_meta["effort"] = effort
        if engine_session_id:
            # Stamp the engine's session id into the result, which the orchestrator
            # records in the session meta for a later native --resume.
            result_meta["session_id"] = engine_session_id
        # Classify a failure from the engine's own output (never our synthetic
        # cancel/timeout text) so `run` can retry transient ones and label the
        # rest. Structured engine error events (e.g. Claude's typed `result`
        # subtype) are included alongside stderr, so classification prefers the
        # engine's own verdict over grepping free text.
        error_text = "\n".join(
            f"{event.data.get('subtype', '')} {event.text}".strip()
            for event in (events or [])
            if event.kind is TranscriptKind.ERROR
        )
        reason = (
            _classify_failure(f"{error_text}\n{stderr_text}\n{text}")
            if (not ok and not cancelled)
            else None
        )
        # Backstop for a limit/refusal whose exact wording we don't match: an
        # instant, output-less, non-zero exit is a hard stop, not a retry.
        if reason is None and not ok and not timed_out and not cancelled and not had_output and elapsed < _EARLY_ABORT_S:
            reason = "aborted_early"
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
                usage_file = getattr(self, "_usage_file", None)
                if usage_file is not None:
                    usage_file.add(event)
            self._after_stream_line(request, sink, stripped)
