"""``horizon commit`` — let an agent record its own work in the workspace ledger.

The workspace ledger is a single out-of-tree git (``.archon-horizon/vcs/workspace.git``)
with hygiene an agent should not have to reproduce by hand: excludes for build
artifacts, a secret-guard hook, nested-``.git`` neutralisation, and machine
provenance trailers. This command wraps a *safe* commit: the agent supplies a
semantic message and the files it changed; Horizon stages them (respecting the
excludes), stamps ``Archon-Run``/``Session``/``Role``/``Task``/``Projects``
trailers from the run env, attributes the commit to the acting role, and
serialises against concurrent runs. It only ever commits — it never resets,
rebases, or rewrites history — so an agent cannot damage the ledger.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import typer

from archon_horizon.core.events import Event
from archon_horizon.log import log
from archon_horizon.store.filesystem import FilesystemEventLog

from .shared import emit_json, load_workspace


def _workspace_rel(files: list[str], root: Path) -> tuple[list[str], list[str]]:
    """Resolve caller-supplied paths (relative to the shell cwd, or absolute) to
    workspace-relative posix paths. Returns ``(inside, outside)`` — paths outside
    the workspace are reported, not committed."""
    root_res = root.resolve()
    inside: list[str] = []
    outside: list[str] = []
    for f in files:
        p = Path(f)
        absolute = p if p.is_absolute() else (Path.cwd() / p)
        try:
            rel = absolute.resolve().relative_to(root_res)
        except ValueError:
            outside.append(f)
            continue
        inside.append(rel.as_posix())
    return inside, outside


def commit(
    ctx: typer.Context,
    files: list[str] = typer.Argument(None, help="Files you changed (relative to your cwd, or absolute). Omit with --changed."),
    message: str = typer.Option(..., "--message", "-m", help="Semantic commit message (math-first: what you proved/built)."),
    changed: bool = typer.Option(False, "--changed", help="Commit ALL your currently-changed project files (excludes shared Horizon state)."),
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Commit your work into the workspace ledger with a semantic message.

    Provenance (run/session/role/task/projects) is stamped automatically from the
    run environment; do not pass it. Commits only your files; the orchestrator
    separately records shared state (roadmap, inbox, …). Safe under concurrent
    runs — the ledger is a single shared branch and commits serialise; if two
    runs touch the same file the later commit simply captures the combined state.
    """
    from archon_horizon.vcs.git import WorkspaceGit, git_available
    from archon_horizon.vcs.integration import _commit_trailers, author_for

    if not git_available():
        log.error("git is not available; cannot commit.")
        raise typer.Exit(1)

    _, workspace = load_workspace(ctx.obj["root"])
    git = WorkspaceGit(workspace.root)
    git.init()

    outside: list[str] = []
    if changed:
        # Everything the agent changed, minus the shared Horizon state the system
        # owns (.archon-horizon) — the agent commits code, not the ledger's books.
        paths = [p for p in git.changed_files() if not p.split("/", 1)[0].startswith(".archon-horizon")]
    else:
        if not files:
            log.error("Pass the files you changed, or --changed to commit all your project changes.")
            raise typer.Exit(1)
        paths, outside = _workspace_rel(list(files), workspace.root)
    if outside:
        log.warn(f"Ignoring path(s) outside the workspace: {', '.join(outside)}")
    if not paths:
        msg = "No changed files to commit."
        emit_json({"committed": None, "reason": "no-changes"}) if as_json else log.info(msg)
        return

    role = os.environ.get("ARCHON_HORIZON_AGENT_ROLE", "").strip().lower() or None
    run_id = os.environ.get("ARCHON_HORIZON_RUN", "").strip()
    session = os.environ.get("ARCHON_HORIZON_SESSION", "").strip()
    task_id = os.environ.get("ARCHON_HORIZON_TASK", "").strip()
    projects = tuple(p for p in os.environ.get("ARCHON_HORIZON_PROJECTS", "").split(",") if p)
    trailers = _commit_trailers(
        run_id=run_id, session=session, role=role or "", round_index=None,
        task_id=task_id, projects=projects,
    )

    try:
        sha = git.commit(message, paths=paths, author=author_for(role), trailers=trailers)
    except Exception as exc:
        log.error(f"Commit failed: {exc}")
        raise typer.Exit(1)

    if sha is None:
        emit_json({"committed": None, "reason": "no-changes"}) if as_json else log.info("Nothing changed in those files; no commit made.")
        return

    files_in = git.files_in_commit(sha)
    # Record the commit as an event so it shows in the run's system log.
    try:
        FilesystemEventLog(workspace.state_path / "events.jsonl").append(Event(
            type="workspace.agent_commit", id=uuid.uuid4().hex, actor=role or "agent",
            data={"run_id": run_id, "session": session, "sha": sha, "message": message.splitlines()[0][:120], "files": list(files_in)},
        ))
    except Exception:
        pass

    if as_json:
        emit_json({"committed": sha, "files": list(files_in)})
        return
    log.success(f"Committed {len(files_in)} file(s) as {sha[:10]}: {message.splitlines()[0][:80]}")
