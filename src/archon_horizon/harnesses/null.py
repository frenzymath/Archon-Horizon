"""A deterministic in-process harness for tests and dry runs.

Returns a canned (or callable-derived) response without launching any
engine, and — like the real harnesses — writes a canonical transcript to the
request's ``artifact_dir`` so the dashboard/server can render it uniformly.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from archon_horizon.transcript.model import TranscriptEvent, TranscriptKind
from archon_horizon.transcript.sink import JsonlTranscriptSink

from .base import Harness, HarnessRequest, HarnessResult, Usage


class NullHarness(Harness):
    name = "null"

    def __init__(
        self,
        responder: Callable[[HarnessRequest], HarnessResult] | str = "",
        *,
        ok: bool = True,
    ) -> None:
        self._responder = responder
        self._ok = ok

    def run(self, request: HarnessRequest) -> HarnessResult:
        if callable(self._responder):
            result = self._responder(request)
        else:
            result = HarnessResult(ok=self._ok, text=self._responder, usage=Usage())

        if request.artifact_dir is not None:
            path = request.artifact_dir / "transcript.jsonl"
            sink = JsonlTranscriptSink(path)
            sink.emit(TranscriptEvent(TranscriptKind.SESSION_START, data={"harness": self.name, "prompt": request.prompt}))
            if result.text:
                sink.emit(TranscriptEvent(TranscriptKind.TEXT, text=result.text))
            sink.emit(TranscriptEvent(TranscriptKind.SESSION_END, data={"ok": result.ok}))
            if not result.artifact_refs:
                result = dataclasses.replace(result, artifact_refs=(path.as_posix(),))
        return result
