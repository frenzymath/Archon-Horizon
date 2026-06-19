"""Subprocess-backed harness — how a real CLI engine plugs in.

One class drives any headless CLI (``claude -p``, ``codex exec``, agy, ...).
The engine-specific details — binary, model, effort, sandbox flags — are
baked into ``argv_template`` when the harness is constructed from its
``harnesses.<name>`` config block, so adding a new engine is "construct a
``CommandHarness`` with the right argv," not a new code path here.

The template is a list of tokens; the literal token ``"{prompt}"`` is
replaced by the request prompt. If no such token is present the prompt is
appended as the final argument.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from .base import Harness, HarnessCapability, HarnessRequest, HarnessResult, Usage

PROMPT_TOKEN = "{prompt}"


class CommandHarness(Harness):
    capabilities = frozenset({HarnessCapability.CANCELLATION})

    def __init__(self, name: str, argv_template: Sequence[str]) -> None:
        self.name = name
        self._argv_template = tuple(argv_template)

    def _argv(self, prompt: str) -> list[str]:
        if PROMPT_TOKEN in self._argv_template:
            return [prompt if tok == PROMPT_TOKEN else tok for tok in self._argv_template]
        return [*self._argv_template, prompt]

    def run(self, request: HarnessRequest) -> HarnessResult:
        if request.cancel is not None and request.cancel.is_cancelled():
            return HarnessResult(ok=False, text="cancelled before start")
        try:
            completed = subprocess.run(
                self._argv(request.prompt),
                cwd=request.cwd,
                capture_output=True,
                text=True,
                timeout=request.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            return HarnessResult(ok=False, text=f"{self.name}: engine not found: {exc}")
        except subprocess.TimeoutExpired as exc:
            return HarnessResult(ok=False, text=f"{self.name}: timed out after {exc.timeout}s")

        ok = completed.returncode == 0
        text = completed.stdout if ok else (completed.stderr or completed.stdout)
        return HarnessResult(
            ok=ok,
            text=text,
            usage=Usage(),
            metadata={"returncode": completed.returncode},
        )
