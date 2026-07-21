"""Expose Horizon's semantic graph through ``horizon graph``.

The graph implementation is shipped inside :mod:`archon_horizon.hgraph`; this
command deliberately forwards its project-level operations instead of invoking
an independently installed ``hgraph`` executable.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from archon_horizon.hgraph.cli import main as graph_main

from .shared import agent_author, agent_provenance, load_workspace


def _with_agent_identity(forwarded: list[str]) -> list[str]:
    """Default authored graph attachments from the same run env as inbox writes."""
    if len(forwarded) < 2 or forwarded[0] != "add" or forwarded[1] not in {"comment", "review"}:
        return forwarded
    enriched = list(forwarded)
    author = agent_author()
    if author and "--author" not in enriched:
        enriched.extend(["--author", author])
    provenance = agent_provenance()
    if provenance and not any(value.startswith("provenance=") for value in enriched):
        enriched.extend(["--set", f"provenance={json.dumps(provenance, separators=(',', ':'))}"])
    return enriched


def _project_root(workspace, project: str | None, cwd: Path) -> Path:
    if project:
        if project not in workspace.projects:
            choices = ", ".join(workspace.projects) or "none configured"
            raise typer.BadParameter(
                f"unknown project {project!r}; available projects: {choices}",
                param_hint="--project",
            )
        return workspace.project_path(project)

    if len(workspace.projects) == 1:
        return workspace.project_path(next(iter(workspace.projects)))

    # Agents commonly run inside one project while ARCHON_HORIZON_ROOT points at
    # the workspace. Resolve that unambiguous project without forcing -p.
    here = cwd.resolve()
    containing = []
    for name in workspace.projects:
        root = workspace.project_path(name).resolve()
        try:
            here.relative_to(root)
        except ValueError:
            continue
        containing.append(root)
    if len(containing) == 1:
        return containing[0]

    choices = ", ".join(workspace.projects) or "none configured"
    raise typer.BadParameter(
        f"select a project with --project; available projects: {choices}",
        param_hint="--project",
    )


def graph(
    ctx: typer.Context,
    project: str | None = typer.Option(
        None, "--project", "-p", help="Project whose semantic graph to use."
    ),
) -> None:
    """Query and edit the vendored semantic graph.

    Examples: ``horizon graph -p MyProject sync``, ``horizon graph -p
    MyProject frontier --type tex``, and ``horizon graph -p MyProject get
    label:my-theorem``.
    """
    forwarded = _with_agent_identity(list(ctx.args))
    if not forwarded or "--help" in forwarded or "-h" in forwarded:
        # Help does not touch the graph, so it remains available even when a
        # multi-project workspace has not selected a project yet.
        try:
            graph_main(forwarded or ["--help"], prog="horizon graph")
        except SystemExit as exc:
            raise typer.Exit(int(exc.code or 0)) from None
        return

    _, workspace = load_workspace(ctx.obj["root"])
    root = _project_root(workspace, project, Path.cwd())
    try:
        code = graph_main(["--root", str(root), *forwarded], prog="horizon graph")
    except SystemExit as exc:
        raise typer.Exit(int(exc.code or 0)) from None
    if code:
        raise typer.Exit(code)
