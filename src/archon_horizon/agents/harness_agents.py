"""Concrete, engine-agnostic agents.

These are the *only* production agent implementations. There is no
per-engine subclass: swapping Claude Code for Codex means handing a
different :class:`~archon_horizon.harnesses.base.Harness` to the same class.
Prompt composition and result parsing are injected (defaulting to the
light, pure functions in this package), so a role's behavior is tuned by
data, not by subclassing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

from archon_horizon.core.tasks import HorizonResult, TaskStatus
from archon_horizon.harnesses.base import Harness, HarnessCapability, HarnessRequest, HarnessResult

from . import parsing, prompts
from .base import (
    HorizonAgent,
    HorizonContext,
    GroundAgent,
    GroundContext,
    GroundUpdate,
)

# Short nudges used in place of the full prompt when continuing a native engine
# session (claude --resume): the engine replays the prior conversation, so the
# original instructions are already in context — re-sending them just duplicates.
HORIZON_CONTINUE = (
    "Continue your previous task from where you left off. Re-read the file(s) you "
    "were editing and any build output if you need to refresh context, then keep "
    "going and finish with the usual brief report."
)
GROUND_CONTINUE = (
    "Continue from where you left off. Re-read the roadmap, the open inbox, and any "
    "report you were writing if you need to refresh context, then finish the round."
)


def _agent_env(role: str, context: HorizonContext | GroundContext) -> dict[str, str]:
    """Env stamped on an agent invocation so its CLI writes carry provenance:
    the role plus the run id and session directory (read back by the `horizon`
    CLI to tag inbox/roadmap/task items with which run/session authored them)."""
    env = {"ARCHON_HORIZON_AGENT_ROLE": role}
    run_id = getattr(context.run, "id", "") or ""
    if run_id:
        env["ARCHON_HORIZON_RUN"] = run_id
    if context.log_dir is not None:
        env["ARCHON_HORIZON_SESSION"] = context.log_dir.name
    return env


def _supports_resume(harness: Harness) -> bool:
    return HarnessCapability.RESUME in getattr(harness, "capabilities", frozenset())


def _result_metadata(result: HarnessResult) -> dict[str, object]:
    """Usage telemetry plus the engine's native session id (when the harness
    captured one), so the orchestrator can record it for a later --resume."""
    meta: dict[str, object] = {
        "usage": {
            "tokens_in": result.usage.tokens_in,
            "tokens_out": result.usage.tokens_out,
            "cost_usd": result.usage.cost_usd,
        }
    }
    session_id = result.metadata.get("session_id")
    if session_id:
        meta["session_id"] = session_id
    for key in ("harness_name", "harness_kind", "model"):
        value = result.metadata.get(key)
        if value:
            meta[key] = value
    return meta


def _harness_metadata(harness: Harness) -> dict[str, object]:
    meta: dict[str, object] = {}
    name = getattr(harness, "horizon_harness_name", None) or getattr(harness, "name", None)
    kind = getattr(harness, "horizon_harness_kind", None)
    model = getattr(harness, "horizon_model", None)
    if name:
        meta["harness_name"] = name
    if kind:
        meta["harness_kind"] = kind
    if model:
        meta["model"] = model
    return meta


def _merge_run_metadata(harness: Harness, result: HarnessResult) -> dict[str, object]:
    """Result metadata plus harness identity. The run's OBSERVED model (read from
    the engine's own stream) wins over the configured one, so the run view shows
    the real model — including when the config never pinned ``--model``."""
    return {**_harness_metadata(harness), **result.metadata}


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
        # Continue the native session only if the engine supports it; otherwise
        # re-run fresh with the full prompt (the engine-agnostic fallback).
        resume = context.resume_session_id if _supports_resume(self._harness) else None
        request = HarnessRequest(
            prompt=HORIZON_CONTINUE if resume else self._compose(context),
            cwd=context.workspace.project_path(context.task.project),
            context_refs=context.previous_report_refs,
            artifact_dir=context.log_dir,
            resume_session_id=resume,
            metadata={"env": _agent_env("horizon", context)},
        )
        result = self._harness.run(request)
        result = dataclasses.replace(result, metadata=_merge_run_metadata(self._harness, result))
        return HorizonResult(
            task_id=context.task.id,
            status=TaskStatus.DONE if result.ok else TaskStatus.FAILED,
            report=result.text,
            artifact_refs=result.artifact_refs,
            metadata=_result_metadata(result),
        )


class HarnessGroundAgent(GroundAgent):
    def __init__(
        self,
        harness: Harness,
        *,
        compose: Callable[[GroundContext], str] = prompts.compose_ground_prompt,
        parse: Callable[[str], GroundUpdate] = parsing.parse_ground_update,
    ) -> None:
        self._harness = harness
        self._compose = compose
        self._parse = parse

    def _run(self, context: GroundContext, prompt: str) -> GroundUpdate:
        # Continue the native session only if the engine supports it; otherwise
        # re-run fresh with the full prompt (the engine-agnostic fallback).
        resume = context.resume_session_id if _supports_resume(self._harness) else None
        request = HarnessRequest(
            prompt=GROUND_CONTINUE if resume else prompt,
            cwd=context.workspace.root,
            artifact_dir=context.log_dir,
            resume_session_id=resume,
            metadata={"env": _agent_env("ground", context)},
        )
        result = self._harness.run(request)
        result = dataclasses.replace(result, metadata=_merge_run_metadata(self._harness, result))
        update = self._parse(result.text)
        metadata = {k: v for k, v in _result_metadata(result).items() if v is not None}
        if metadata:
            update = dataclasses.replace(update, metadata={**update.metadata, **metadata})
        return update

    def run_round(self, context: GroundContext) -> GroundUpdate:
        return self._run(context, self._compose(context))

    def handle_horizon_result(
        self,
        context: GroundContext,
        result: HorizonResult,
    ) -> GroundUpdate:
        prompt = (
            self._compose(context)
            + f"\n\n# Horizon result for {result.task_id} ({result.status})\n{result.report}"
        )
        return self._run(context, prompt)
