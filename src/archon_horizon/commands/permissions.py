"""``horizon permissions`` — the standing delegation consent an agent may read.

A running agent (a "team") reads this before it considers delegating work it
cannot do inline: creating new tasks, or launching a whole new ``horizon run``.
The default is fully closed. The config block is free-form, so the human can
also record account/api notes (limit-reset times, which config dir to use) that
the agent reads and reasons about. Reading this authorizes nothing on its own.
"""

from __future__ import annotations

import typer

from archon_horizon.log import log

from .shared import emit_json, load_workspace

app = typer.Typer(help="Read the workspace's delegation permissions.", no_args_is_help=False, invoke_without_command=True)


def _delegation_dict(deleg) -> dict:
    return {
        "allow_launch_tasks": deleg.allow_launch_tasks,
        "allow_launch_runs": deleg.allow_launch_runs,
        "max_parallel_sessions": deleg.max_parallel_sessions,
        "accounts": [dict(a) for a in deleg.accounts],
        "raw": dict(deleg.raw),
    }


def permissions(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout."),
) -> None:
    """Show whether (and how) this workspace permits an agent to delegate.

    Default is deny: an agent may NOT create tasks or launch runs. Enable in
    ``config.yaml`` under ``workspace.delegation`` (see the `horizon` skill).
    """
    cfg, _ = load_workspace(ctx.obj["root"])
    deleg = cfg.delegation
    if as_json:
        emit_json(_delegation_dict(deleg))
        return
    log.key_value(
        {
            "launch tasks": "allowed" if deleg.allow_launch_tasks else "denied (default)",
            "launch runs": "allowed" if deleg.allow_launch_runs else "denied (default)",
            "max parallel sessions": str(deleg.max_parallel_sessions or "—"),
            "accounts declared": str(len(deleg.accounts)),
        },
        title="Delegation permissions",
    )
    if not (deleg.allow_launch_tasks or deleg.allow_launch_runs):
        log.info("Delegation is off. Do not create tasks or launch runs on the user's behalf.")
    extra = {k: v for k, v in deleg.raw.items()
             if k not in {"allow_launch_tasks", "allow_launch_runs", "max_parallel_sessions", "accounts"}}
    if extra:
        log.key_value({k: str(v) for k, v in extra.items()}, title="Notes (free-form)")
