"""Agent-ledger hygiene: inspect and prune non-source paths from the journal."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from archon_horizon.commands.shared import emit_json, load_workspace
from archon_horizon.log import log
from archon_horizon.vcs.git import GitError, WorkspaceGit, git_available

app = typer.Typer(
    help=(
        "Agent source ledger (out-of-tree workspace.git). "
        "Lean/blueprint/config history for agents — not Horizon state or hgraph."
    ),
    no_args_is_help=True,
)


def _ensure_workspace(root: Path) -> None:
    """Resolve workspace config; raise Typer exit if this is not a Horizon root."""
    try:
        load_workspace(root)
    except Exception as exc:
        raise typer.Exit(f"not a Horizon workspace at {root}: {exc}") from exc


def _summarize_prefixes(paths: list[str], *, limit: int = 12) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for path in paths:
        if path.startswith(".archon-horizon/"):
            parts = path.split("/", 2)
            key = "/".join(parts[:2]) if len(parts) >= 2 else path
        elif "/hgraph/" in path or path.endswith("/hgraph") or path == "hgraph":
            key = path.split("/hgraph/")[0] + "/hgraph/" if "/hgraph/" in path else "hgraph/"
        else:
            key = path.split("/", 1)[0]
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


@app.command("status")
def ledger_status(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show how many tracked ledger paths are state/hgraph noise vs sources."""
    root: Path = ctx.obj["root"]
    _ensure_workspace(root)
    if not git_available():
        raise typer.Exit("git is not available")
    git = WorkspaceGit(root)
    if not git.is_repo():
        payload = {"repo": False, "non_ledger": 0, "head": None}
        if as_json:
            emit_json(payload)
        else:
            log.info("No workspace ledger yet (`.archon-horizon/vcs/workspace.git`).")
        return
    git.init()  # refresh excludes
    victims = list(git.list_non_ledger_tracked_paths())
    head = git.current_sha()
    payload: dict[str, Any] = {
        "repo": True,
        "head": head,
        "non_ledger": len(victims),
        "prefixes": [
            {"prefix": p, "count": n} for p, n in _summarize_prefixes(victims)
        ],
        "sample": victims[:15],
    }
    if as_json:
        emit_json(payload)
        return
    log.info(f"Ledger HEAD: {head[:12] if head else '(empty)'}")
    if not victims:
        log.success("Ledger index matches source-only policy (no state/hgraph tracked).")
        return
    log.warn(
        f"{len(victims)} tracked path(s) are Horizon state or generated hgraph — "
        "not agent source history."
    )
    for prefix, count in _summarize_prefixes(victims):
        log.step(f"  {count:>7}  {prefix}")
    log.step("Run `horizon ledger prune` to drop them from the ledger (files stay on disk).")
    log.step("Add `--gc` after prune to reclaim pack space from old blobs.")


@app.command("prune")
def ledger_prune(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="List paths that would be untracked; do not commit."
    ),
    gc: bool = typer.Option(
        False,
        "--gc",
        help="After a real prune, run `git gc --prune=now` to drop unreachable blobs.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Drop `.archon-horizon/` and `**/hgraph/` from the ledger index (keep on disk).

    Creates one ledger commit that removes those paths from HEAD. Working-tree
    files are untouched — inbox, runs, and local hgraph still work for the
    dashboard. Historical blobs remain until you pass ``--gc`` (rewrites packs;
    do this when no other run is mid-commit).
    """
    root: Path = ctx.obj["root"]
    _ensure_workspace(root)
    if not git_available():
        raise typer.Exit("git is not available")
    git = WorkspaceGit(root)
    if not git.is_repo():
        raise typer.Exit("No workspace ledger at `.archon-horizon/vcs/workspace.git`.")
    try:
        summary = git.prune_non_ledger_paths(dry_run=dry_run, gc=gc and not dry_run)
    except GitError as exc:
        raise typer.Exit(f"ledger prune failed: {exc}") from exc
    if as_json:
        emit_json(summary)
        return
    n = int(summary.get("paths") or 0)
    if n == 0:
        log.success("Nothing to prune — ledger already source-only.")
        return
    if dry_run:
        log.info(f"Would drop {n} path(s) from the ledger index (dry-run).")
        for prefix, count in _summarize_prefixes(list(summary.get("sample") or [])):
            log.step(f"  sample prefix {prefix} (among first paths)")
        for path in list(summary.get("sample") or [])[:10]:
            log.step(f"  {path}")
        log.step("Re-run without --dry-run to commit the index cleanup.")
        return
    sha = str(summary.get("sha") or "")
    log.success(
        f"Pruned {n} non-ledger path(s) from the agent journal"
        + (f" ({sha[:12]})" if sha else "")
        + ". Working tree unchanged."
    )
    if summary.get("gc"):
        log.info("Ran git gc --prune=now on the ledger.")
    elif not gc:
        log.step("Optional: `horizon ledger prune --gc` later to reclaim pack space.")
