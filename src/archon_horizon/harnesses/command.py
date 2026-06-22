"""Subprocess-backed harness — how a real CLI engine plugs in.

One class drives any headless CLI (``claude -p``, ``codex exec``, agy, ...).
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

import queue
import subprocess
import threading
import time
from collections.abc import Sequence

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.parsers import TranscriptParser, aggregate, parse_plain_line
from archon_horizon.transcript.sink import JsonlTranscriptSink, NullTranscriptSink, TranscriptSink

from .base import Harness, HarnessCapability, HarnessRequest, HarnessResult

PROMPT_TOKEN = "{prompt}"
_POLL_S = 0.2


def _refs(ref: str | None) -> tuple[str, ...]:
    return (ref,) if ref else ()


class CommandHarness(Harness):
    capabilities = frozenset({HarnessCapability.CANCELLATION, HarnessCapability.STREAMING})

    def __init__(
        self,
        name: str,
        argv_template: Sequence[str],
        *,
        parser: TranscriptParser = parse_plain_line,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._argv_template = tuple(argv_template)
        self._parser = parser
        self.env_overrides = dict(env_overrides) if env_overrides else {}

    def _argv(self, prompt: str) -> list[str]:
        if PROMPT_TOKEN in self._argv_template:
            return [prompt if tok == PROMPT_TOKEN else tok for tok in self._argv_template]
        return [*self._argv_template, prompt]

    def run(self, request: HarnessRequest) -> HarnessResult:
        ref: str | None = None
        sink: TranscriptSink = NullTranscriptSink()
        if request.artifact_dir is not None:
            path = request.artifact_dir / "transcript.jsonl"
            sink = JsonlTranscriptSink(path)
            ref = path.as_posix()
        sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"harness": self.name}))

        if request.cancel is not None and request.cancel.is_cancelled():
            sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": False, "reason": "cancelled"}))
            return HarnessResult(ok=False, text="cancelled before start", artifact_refs=_refs(ref))

        try:
            import os
            env = dict(os.environ)
            if self.env_overrides:
                env.update(self.env_overrides)
            proc = subprocess.Popen(
                self._argv(request.prompt),
                cwd=request.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            sink.emit(TranscriptEvent(TranscriptKind.ERROR, text=str(exc)))
            sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": False}))
            return HarnessResult(ok=False, text=f"{self.name}: engine not found: {exc}", artifact_refs=_refs(ref))

        events, stderr_lines = self._stream(proc, request, sink)

        timed_out = events is None
        cancelled = request.cancel is not None and request.cancel.is_cancelled()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        returncode = proc.returncode if proc.returncode is not None else -1
        ok = returncode == 0 and not timed_out and not cancelled

        text, usage = aggregate(events or [])
        if not ok and not text:
            text = "".join(stderr_lines).strip() or (
                "timed out" if timed_out else "cancelled" if cancelled else f"exit {returncode}"
            )
        sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": ok, "returncode": returncode}))
        return HarnessResult(
            ok=ok,
            text=text,
            usage=usage,
            artifact_refs=_refs(ref),
            metadata={"returncode": returncode, "timed_out": timed_out, "cancelled": cancelled},
        )

    def _stream(
        self, proc: subprocess.Popen, request: HarnessRequest, sink: TranscriptSink
    ) -> tuple[list[TranscriptEvent] | None, list[str]]:
        """Pump stdout through the parser. Returns (events, stderr); events is
        None if the run timed out (so the caller can flag it)."""
        out_q: queue.Queue[str | None] = queue.Queue()
        stderr_lines: list[str] = []

        def pump_stdout() -> None:
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    out_q.put(line)
            finally:
                out_q.put(None)

        def drain_stderr() -> None:
            if proc.stderr is not None:
                stderr_lines.extend(proc.stderr)

        threading.Thread(target=pump_stdout, daemon=True).start()
        threading.Thread(target=drain_stderr, daemon=True).start()

        events: list[TranscriptEvent] = []
        deadline = time.monotonic() + request.timeout_s if request.timeout_s else None
        while True:
            if request.cancel is not None and request.cancel.is_cancelled():
                proc.terminate()
                return events, stderr_lines
            if deadline is not None and time.monotonic() > deadline:
                proc.terminate()
                return None, stderr_lines
            try:
                line = out_q.get(timeout=_POLL_S)
            except queue.Empty:
                continue
            if line is None:
                return events, stderr_lines
            for event in self._parser(line.rstrip("\n")):
                sink.emit(event)
                events.append(event)
