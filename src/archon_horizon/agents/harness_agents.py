"""Concrete, engine-agnostic agents.

These are the *only* production agent implementations. There is no
per-engine subclass: swapping Claude Code for Codex or agy means handing a
different :class:`~archon_horizon.harnesses.base.Harness` to the same class.
Prompt composition and result parsing are injected (defaulting to the
light, pure functions in this package), so a role's behavior is tuned by
data, not by subclassing.
"""

from __future__ import annotations

from collections.abc import Callable

from archon_horizon.core.tasks import HorizonResult, TaskStatus
from archon_horizon.harnesses.base import Harness, HarnessRequest, HarnessResult

from . import parsing, prompts
from .base import (
    HorizonAgent,
    HorizonContext,
    InformalAgent,
    InformalContext,
    InformalUpdate,
)


def _usage_metadata(result: HarnessResult) -> dict[str, object]:
    return {
        "usage": {
            "tokens_in": result.usage.tokens_in,
            "tokens_out": result.usage.tokens_out,
            "cost_usd": result.usage.cost_usd,
        }
    }


class HarnessHorizonAgent(HorizonAgent):
    def __init__(
        self,
        harness: Harness,
        *,
        compose: Callable[[HorizonContext], str] = prompts.compose_horizon_prompt,
    ) -> None:
        self._harness = harness
        self._compose = compose

    def run_task(self, context: HorizonContext) -> HorizonResult:
        request = HarnessRequest(
            prompt=self._compose(context),
            cwd=context.workspace.project_path(context.task.project),
            context_refs=context.previous_report_refs,
            artifact_dir=context.workspace.artifact_dir(context.run.id, context.task.id),
        )
        result = self._harness.run(request)
        return HorizonResult(
            task_id=context.task.id,
            status=TaskStatus.DONE if result.ok else TaskStatus.FAILED,
            report=result.text,
            artifact_refs=result.artifact_refs,
            metadata=_usage_metadata(result),
        )


class HarnessInformalAgent(InformalAgent):
    def __init__(
        self,
        harness: Harness,
        *,
        compose: Callable[[InformalContext], str] = prompts.compose_informal_prompt,
        parse: Callable[[str], InformalUpdate] = parsing.parse_informal_update,
    ) -> None:
        self._harness = harness
        self._compose = compose
        self._parse = parse

    def _run(self, context: InformalContext, prompt: str) -> InformalUpdate:
        request = HarnessRequest(
            prompt=prompt,
            cwd=context.workspace.root,
            artifact_dir=context.workspace.artifact_dir(context.run.id, "informal"),
        )
        result = self._harness.run(request)
        update = self._parse(result.text)
        return update

    def run_round(self, context: InformalContext) -> InformalUpdate:
        return self._run(context, self._compose(context))

    def handle_horizon_result(
        self,
        context: InformalContext,
        result: HorizonResult,
    ) -> InformalUpdate:
        prompt = (
            self._compose(context)
            + f"\n\n# Horizon result for {result.task_id} ({result.status})\n{result.report}"
        )
        return self._run(context, prompt)
