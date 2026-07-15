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

# Preambles prepended to the FULL prompt when continuing a native engine session
# (claude --resume). The engine is *supposed* to replay the prior conversation, so
# in theory the original instructions are already in context — but that replay is
# fragile (a pruned/branched session, a different CLAUDE_CONFIG_DIR, or an engine
# that silently starts fresh leaves the agent with a bare "continue" and no idea
# what it was doing). So we always re-send the full prompt too: duplication when
# the replay worked is cheap; missing context when it didn't is a wasted session.
HORIZON_CONTINUE = (
    "You are RESUMING a task you started earlier — its full instructions follow "
    "below. If your prior conversation is already in context, pick up from where you "
    "left off (re-read the file(s) you were editing and any build output to refresh); "
    "otherwise start from the instructions below. Finish with the usual brief report.\n\n"
    "─────────────────────────────────────────\n\n"
)
GROUND_CONTINUE = (
    "You are RESUMING this round — its full instructions follow below. If your prior "
    "conversation is already in context, pick up from where you left off (re-read the "
    "roadmap, the open inbox, and any report you were writing); otherwise start from "
    "the instructions below. Then finish the round.\n\n"
    "─────────────────────────────────────────\n\n"
)


def _resume_failed_to_start(result: HarnessResult) -> bool:
    """True when a native ``--resume`` couldn't restart the conversation at all.

    A resumed session whose engine conversation is gone (pruned, or created under
    a different ``CLAUDE_CONFIG_DIR``) exits instantly with no output — classified
    as ``aborted_early``. That's a hard-fatal reason upstream, so without a
    fallback the whole run halts. We detect exactly that "couldn't even start"
    signature (not a resumed session that did real work and then failed) so the
    caller can retry once as a fresh, full-prompt session instead of failing.
    """
    return not result.ok and result.metadata.get("failure_reason") == "aborted_early"


def _agent_env(role: str, context: HorizonContext | GroundContext) -> dict[str, str]:
    """Env stamped on an agent invocation so its CLI writes carry provenance:
    the role plus the run id and session directory (read back by the `horizon`
    CLI to tag inbox/roadmap/task items with which run/session authored them).

    Also carries ``ARCHON_HORIZON_ROOT`` so a `horizon` sub-invocation resolves
    the workspace even when the agent's shell cwd is a project subdirectory (the
    Horizon agent runs from its project dir), and the task/projects so a
    ``horizon commit`` the agent makes carries full provenance trailers."""
    env = {"ARCHON_HORIZON_AGENT_ROLE": role}
    env["ARCHON_HORIZON_ROOT"] = str(context.workspace.root.resolve())
    run_id = getattr(context.run, "id", "") or ""
    if run_id:
        env["ARCHON_HORIZON_RUN"] = run_id
    if context.log_dir is not None:
        env["ARCHON_HORIZON_SESSION"] = context.log_dir.name
    task = getattr(context, "task", None)
    if task is not None:
        env["ARCHON_HORIZON_TASK"] = getattr(task, "id", "") or ""
        projects = getattr(task, "projects", None) or ((task.project,) if getattr(task, "project", None) else ())
        if projects:
            env["ARCHON_HORIZON_PROJECTS"] = ",".join(projects)
    return env


def _supports_resume(harness: Harness) -> bool:
    return HarnessCapability.RESUME in getattr(harness, "capabilities", frozenset())


def _result_metadata(result: HarnessResult) -> dict[str, object]:
    """Usage telemetry plus the engine's native session id (when the harness
    captured one), so the orchestrator can record it for a later --resume.

    Also carries the engine's exit disposition (``ok`` and, on failure, its
    ``returncode`` / ``failure_reason``) so a crashed session is surfaced rather
    than silently recorded as healthy — the Ground path in particular parses the
    (possibly stub) report text and would otherwise lose the exit-1 signal."""
    meta: dict[str, object] = {
        "ok": result.ok,
        "usage": {
            "tokens_in": result.usage.tokens_in,
            "tokens_out": result.usage.tokens_out,
            "cost_usd": result.usage.cost_usd,
        },
    }
    session_id = result.metadata.get("session_id")
    if session_id:
        meta["session_id"] = session_id
    if not result.ok:
        for key in ("returncode", "failure_reason", "timed_out", "retries_exhausted"):
            value = result.metadata.get(key)
            if value is not None:
                meta[key] = value
    for key in ("harness_name", "harness_kind", "model", "effort", "config_dir", "auth"):
        value = result.metadata.get(key)
        if value:
            meta[key] = value
    return meta


def _harness_metadata(harness: Harness) -> dict[str, object]:
    meta: dict[str, object] = {}
    name = getattr(harness, "horizon_harness_name", None) or getattr(harness, "name", None)
    kind = getattr(harness, "horizon_harness_kind", None)
    model = getattr(harness, "horizon_model", None)
    effort = getattr(harness, "horizon_effort", None)
    config_dir = getattr(harness, "horizon_config_dir", None)
    auth = getattr(harness, "horizon_auth", None)
    if name:
        meta["harness_name"] = name
    if kind:
        meta["harness_kind"] = kind
    if model:
        meta["model"] = model
    if effort:
        meta["effort"] = effort
    if config_dir:
        meta["config_dir"] = config_dir
    if auth:
        meta["auth"] = auth
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

    def harness_metadata(self) -> dict[str, object]:
        """Harness/model/effort/auth this agent will run with — stamped into the
        session meta at START so the live dashboard reflects the real config (e.g.
        ``ultracode``) immediately, not only once the session finalizes."""
        return _harness_metadata(self._harness)

    def run_task(self, context: HorizonContext) -> HorizonResult:
        # Continue the native session only if the engine supports it; otherwise
        # re-run fresh with the full prompt (the engine-agnostic fallback).
        resume = context.resume_session_id if _supports_resume(self._harness) else None
        full_prompt = self._compose(context)
        request = HarnessRequest(
            prompt=(HORIZON_CONTINUE + full_prompt) if resume else full_prompt,
            cwd=context.workspace.project_path(context.task.project),
            context_refs=context.previous_report_refs,
            artifact_dir=context.log_dir,
            resume_session_id=resume,
            cancel=context.cancel,
            metadata={"env": _agent_env("horizon", context)},
        )
        result = self._harness.run(request)
        if resume and _resume_failed_to_start(result):
            # The native session was gone; retry once fresh with the full prompt
            # (the same fallback the engine-unsupported path takes) rather than
            # letting the aborted resume halt the run.
            result = self._harness.run(dataclasses.replace(
                request, prompt=self._compose(context), resume_session_id=None,
            ))
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

    def harness_metadata(self) -> dict[str, object]:
        """Harness/model/effort/auth this agent will run with (see the Horizon
        agent's version) — stamped into the session meta at START."""
        return _harness_metadata(self._harness)

    def _run(self, context: GroundContext, prompt: str) -> GroundUpdate:
        # Continue the native session only if the engine supports it; otherwise
        # re-run fresh with the full prompt (the engine-agnostic fallback).
        resume = context.resume_session_id if _supports_resume(self._harness) else None
        request = HarnessRequest(
            prompt=(GROUND_CONTINUE + prompt) if resume else prompt,
            cwd=context.workspace.root,
            artifact_dir=context.log_dir,
            resume_session_id=resume,
            metadata={"env": _agent_env("ground", context)},
        )
        result = self._harness.run(request)
        if resume and _resume_failed_to_start(result):
            # The native session was gone; retry once fresh with the full prompt
            # rather than letting the aborted resume halt the run.
            result = self._harness.run(dataclasses.replace(
                request, prompt=prompt, resume_session_id=None,
            ))
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
