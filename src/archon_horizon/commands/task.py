"""Typer command group for tasks — safe YAML writes, like inbox and roadmap.

Tasks are the human's lever for launching sessions (`horizon run`); the rest are
derived from the roadmap by the orchestrator. Agents never author tasks — they
organize pending work through the roadmap — so ``add``/``set``/``remove`` refuse
the ground/horizon agent (see :func:`refuse_agents`); only ``comment`` is open to
them, and it has no effect on what runs. They live as YAML files under
``.archon-horizon/tasks/``; every write here goes through the store's
``yaml.safe_dump``, so it is always valid no matter what text is passed.
"""

from __future__ import annotations

import dataclasses

import typer

from archon_horizon.core.clock import utc_now
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.store import serde
from archon_horizon.log import log

from .shared import agent_author, emit_json, history_entry, load_workspace, refuse_agents, task_store, with_provenance

app = typer.Typer(help="Read and update Horizon tasks (safe YAML writes).", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _store(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return task_store(workspace)


def _task_dict(task: HorizonTask) -> dict:
    return {
        "id": task.id,
        "project": task.project,
        "projects": list(task.projects),
        "title": task.title,
        "objective": task.objective,
        "status": task.status.value,
        "priority": task.priority,
        "write_set": {
            "files": list(task.write_set.files),
            "projects": list(task.write_set.projects),
            "declarations": list(task.write_set.declarations),
            "blueprint_nodes": list(task.write_set.blueprint_nodes),
            "workspace": task.write_set.workspace,
        },
        "scope": serde.to_jsonable(task.scope),
        "roadmap_refs": list(task.roadmap_refs),
        "inbox_refs": list(task.inbox_refs),
    }


@app.command("list")
def list_tasks(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """List tasks."""
    tasks = _store(ctx).list()
    if as_json:
        emit_json({"tasks": [_task_dict(t) for t in tasks]})
        return
    if not tasks:
        log.info("No tasks.")
        return
    log.results_table([(t.id, t.status.value, t.title or t.objective[:48]) for t in tasks], title="Tasks")


@app.command("show")
def show_task(ctx: typer.Context, task_id: str = typer.Argument(...), as_json: bool = _JSON) -> None:
    """Show one task in full."""
    store = _store(ctx)
    try:
        task = store.get(task_id)
    except Exception:
        log.error(f"No task {task_id!r}.")
        raise typer.Exit(1)
    emit_json(_task_dict(task)) if as_json else log.info(str(_task_dict(task)))


@app.command("set")
def set_task(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task id."),
    status: str | None = typer.Option(None, "--status", help="queued|running|blocked|done|failed|cancelled."),
    priority: str | None = typer.Option(None, "--priority", help="urgent|high|normal|low."),
    objective: str | None = typer.Option(None, "--objective"),
    title: str | None = typer.Option(None, "--title"),
    author: str | None = typer.Option(None, "--author", help="Who is making the change (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Update fields of an existing task (safe YAML write). Human-only."""
    refuse_agents("edit a task")
    store = _store(ctx)
    try:
        task = store.get(task_id)
    except Exception:
        log.error(f"No task {task_id!r}.")
        raise typer.Exit(1)
    actor = author or agent_author() or task.metadata.get("author")
    changes: dict[str, object] = {"updated_at": utc_now()}
    if status is not None:
        changes["status"] = TaskStatus(status.lower())
        store.append_history(task_id, history_entry(actor, "status",
                             before=task.status.value, after=status.lower()))
    if priority is not None:
        changes["priority"] = priority
    if objective is not None:
        changes["objective"] = objective
    if title is not None:
        changes["title"] = title
    edited = [f for f in changes if f not in ("status", "updated_at")]
    if edited:
        store.append_history(task_id, history_entry(actor, "edited", note=", ".join(edited) + " updated"))
    updated = store.put(dataclasses.replace(task, **changes))
    emit_json(_task_dict(updated)) if as_json else log.success(f"Updated task {task_id}.")


@app.command("add")
def add_task(
    ctx: typer.Context,
    task_id: str = typer.Option(..., "--id", help="New task id."),
    project: str = typer.Option(..., "--project", help="Primary project."),
    objective: str = typer.Option(..., "--objective", help="What the task should accomplish."),
    title: str = typer.Option("", "--title"),
    projects: list[str] = typer.Option(None, "--projects", help="All projects the task spans (repeatable)."),
    files: list[str] = typer.Option(None, "--file", help="Write-scope file glob (repeatable)."),
    priority: str = typer.Option("normal", "--priority"),
    status: str = typer.Option("queued", "--status"),
    author: str | None = typer.Option(None, "--author", help="Who is adding the task (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Add a new task (safe YAML write). Human-only."""
    refuse_agents("add a task")
    store = _store(ctx)
    actor = author or agent_author("human")
    project_tuple = tuple(projects) if projects else (project,)
    task = HorizonTask(
        id=task_id,
        project=project,
        objective=objective,
        title=title or objective,
        projects=project_tuple,
        priority=priority,
        status=TaskStatus(status.lower()),
        write_set=WriteSet(projects=project_tuple, files=tuple(files or ())),
        scope=ItemScope(projects=project_tuple, files=tuple(files or ())),
        metadata=with_provenance({"author": actor} if actor else {}),
    )
    store.put(task)
    store.append_history(task_id, history_entry(actor, "created", after=task.status.value, note="opened"))
    emit_json(_task_dict(task)) if as_json else log.success(f"Added task {task_id}.")


@app.command("comment")
def comment_task(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task id."),
    body: str = typer.Option(..., "--body", help="Comment body (Markdown)."),
    author: str | None = typer.Option(None, "--author", help="Comment author (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Add a progress comment to a task (record a key advance)."""
    store = _store(ctx)
    try:
        store.get(task_id)
    except Exception:
        log.error(f"No task {task_id!r}.")
        raise typer.Exit(1)
    store.add_comment(task_id, body, author or agent_author())
    if as_json:
        emit_json({"id": task_id, "commented": True})
        return
    log.success(f"Commented on task {task_id}.")


@app.command("remove")
def remove_task(ctx: typer.Context, task_id: str = typer.Argument(...)) -> None:
    """Remove a task (deletes its YAML file). Human-only."""
    refuse_agents("remove a task")
    _store(ctx).delete(task_id)
    log.success(f"Removed task {task_id}.")
