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
from pathlib import Path

from archon_horizon.core.tasks import HorizonResult, TaskStatus
from archon_horizon.harnesses.base import Harness, HarnessCapability, HarnessRequest, HarnessResult

from . import prompts
from .base import HorizonAgent, HorizonContext

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


def _agent_env(role: str, context: HorizonContext) -> dict[str, str]:
    """Env stamped on an agent invocation so its CLI writes carry provenance:
    the role plus the run id and session directory (read back by the `horizon`
    CLI to tag inbox/roadmap/task items with which run/session authored them).

    Also carries ``ARCHON_HORIZON_ROOT`` so a `horizon` sub-invocation resolves
    the workspace even when the agent's shell cwd is a project subdirectory (the
    Horizon agent runs from its project dir), and the task/projects so a raw ``git``
    commit the agent makes into the ledger carries full provenance trailers (stamped
    by the ledger's prepare-commit-msg hook)."""
    env = {
        "ARCHON_HORIZON_AGENT_ROLE": role,
        # A headless run may be launched from an interactive Horizon shell. Do not
        # inherit that parent's marker or Stop would skip the final-report guard.
        "ARCHON_HORIZON_INTERACTIVE": "0",
    }
    env["ARCHON_HORIZON_ROOT"] = str(context.workspace.root.resolve())
    env["ARCHON_HORIZON_SKILL"] = str(
        (context.workspace.root / ".claude" / "skills" / "horizon" / "SKILL.md").resolve()
    )
    run_id = getattr(context.run, "id", "") or ""
    if run_id:
        env["ARCHON_HORIZON_RUN"] = run_id
    if context.log_dir is not None:
        env["ARCHON_HORIZON_SESSION"] = context.log_dir.name
        env["ARCHON_HORIZON_SESSION_DIR"] = str(context.log_dir.resolve())
    if context.round_index is not None:
        env["ARCHON_HORIZON_ROUND"] = str(context.round_index)
    if context.rounds_total is not None:
        env["ARCHON_HORIZON_ROUNDS"] = str(context.rounds_total)
    task = getattr(context, "task", None)
    if task is not None:
        env["ARCHON_HORIZON_TASK"] = getattr(task, "id", "") or ""
        title = getattr(task, "title", "") or ""
        if title:
            env["ARCHON_HORIZON_TASK_TITLE"] = title
        projects = getattr(task, "projects", None) or ((task.project,) if getattr(task, "project", None) else ())
        if projects:
            env["ARCHON_HORIZON_PROJECTS"] = ",".join(projects)
    # Route Python, Lean tooling, downloads, and other temp-aware subprocesses
    # away from a quota-constrained shared /tmp.  The directory is per run and
    # session; it is disposable and reclaimed after this invocation below.
    from archon_horizon.core.scratch import scratch_environment

    _, scratch_env = scratch_environment(
        context.workspace,
        run_id=run_id,
        session=context.log_dir.name if context.log_dir is not None else None,
        role=role,
    )
    env.update(scratch_env)
    # Ledger access for plain-git commits (see the project-git skill). We expose
    # the ledger paths + a thin `hgit` passthrough rather than exporting
    # GIT_DIR/GIT_WORK_TREE, which would redirect `lake` and the project's own git.
    # The prepare-commit-msg hook stamps provenance trailers from the ARCHON_* env
    # above, so a raw `git commit` still maps to its session/task in the dashboard.
    from archon_horizon.vcs.git import WorkspaceGit, install_ledger_git_wrapper
    env["HORIZON_LEDGER_GIT_DIR"] = str(WorkspaceGit(context.workspace.root).git_dir.resolve())
    env["HORIZON_LEDGER_WORK_TREE"] = str(context.workspace.root.resolve())
    wrapper = install_ledger_git_wrapper(context.workspace.state_path)
    if wrapper is not None:
        env["HORIZON_GIT"] = str(wrapper.resolve())
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
        for key in ("returncode", "failure_reason", "timed_out", "retries_exhausted", "retry_after_s"):
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
        compose: Callable[[HorizonContext], str] = prompts.horizon_task_prompt,
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
        env = _agent_env("horizon", context)
        scratch_dir = env.get("ARCHON_HORIZON_TMP")
        request = HarnessRequest(
            prompt=(HORIZON_CONTINUE + full_prompt) if resume else full_prompt,
            cwd=context.workspace.project_path(context.task.project),
            artifact_dir=context.log_dir,
            resume_session_id=resume,
            cancel=context.cancel,
            metadata={"env": env},
        )
        try:
            result = self._harness.run(request)
            if resume and _resume_failed_to_start(result):
                # The native session was gone; retry once fresh with the full prompt
                # (the same fallback the engine-unsupported path takes) rather than
                # letting the aborted resume halt the run.
                result = self._harness.run(dataclasses.replace(
                    request, prompt=self._compose(context), resume_session_id=None,
                ))
        finally:
            if scratch_dir:
                from archon_horizon.core.scratch import remove_session_tmp

                remove_session_tmp(Path(scratch_dir))
        result = dataclasses.replace(result, metadata=_merge_run_metadata(self._harness, result))
        return HorizonResult(
            task_id=context.task.id,
            status=TaskStatus.DONE if result.ok else TaskStatus.FAILED,
            report=result.text,
            artifact_refs=result.artifact_refs,
            metadata=_result_metadata(result),
        )
