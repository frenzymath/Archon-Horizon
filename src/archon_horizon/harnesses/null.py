"""A deterministic in-process harness for tests and dry runs.

Returns a canned (or callable-derived) response without launching any
engine. Because the engine seam is :class:`Harness`, the whole
orchestration can be exercised end-to-end with this in place of Claude
Code / Codex / agy.
"""

from __future__ import annotations

from collections.abc import Callable

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
            return self._responder(request)
        return HarnessResult(ok=self._ok, text=self._responder, usage=Usage())
