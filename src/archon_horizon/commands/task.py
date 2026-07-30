"""Typer command group for tasks — safe YAML writes, like inbox and roadmap.

Tasks are the human's lever for launching sessions (`horizon run <task_id>`), or
inferred on demand from a roadmap milestone (`horizon run <roadmap_id>`). Agents
have full access here — they may ``add``, ``set`` (including status), ``comment``,
and ``remove`` — and are encouraged to leave a ``comment`` when they hit a
significant step and to update ``status`` themselves (``done`` only when the work
is *fully* complete). The machine (scheduler/orchestrator) only ever writes
``queued``/``running``; every terminal status (``done``/``blocked``/``failed``) is
the agent's own word, recorded here. Writes are stamped with agent provenance so
the ledger shows who changed what. They live as YAML files under
``.archon-horizon/tasks/``; every write here goes through the store's
``yaml.safe_dump``, so it is always valid no matter what text is passed.
"""

from __future__ import annotations

import dataclasses

import typer

from archon_horizon.core.clock import utc_now
from archon_horizon.core.collection_health import roadmap_health_warnings, task_health_warnings
from archon_horizon.core.roadmap import Roadmap
from archon_horizon.core.scope import ItemScope
from archon_horizon.core.status_sync import roadmap_status_for_task_status
from archon_horizon.core.tasks import HorizonTask, TaskStatus, WriteSet
from archon_horizon.store import serde
from archon_horizon.log import log

from .shared import agent_author, emit_json, ensure_concise_agent_message, history_entry, load_workspace, provenance_project, roadmap_store, task_store, with_provenance

app = typer.Typer(help="Read and update Horizon tasks (safe YAML writes).", no_args_is_help=True)

_JSON = typer.Option(False, "--json", help="Emit machine-readable JSON to stdout.")


def _store(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return task_store(workspace)


def _stores(ctx: typer.Context):
    _, workspace = load_workspace(ctx.obj["root"])
    return task_store(workspace), roadmap_store(workspace)


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
        # Mirror `inbox show --json`, which returns comments: a lane that leaves a
        # task comment and reads it back must see it, rather than concluding the
        # write failed because the key was absent (I-0618).
        "comments": list(task.metadata.get("comments", [])),
    }


def _warn_tasks(tasks) -> list[str]:
    warnings = task_health_warnings(tasks)
    for warning in warnings:
        log.warn(warning)
    return warnings


def _with_task_warnings(payload: dict, tasks) -> dict:
    """Keep legacy JSON shapes when healthy; attach actionable warnings only."""
    warnings = task_health_warnings(tasks)
    return {**payload, **({"warnings": warnings} if warnings else {})}


@app.command("list")
def list_tasks(ctx: typer.Context, as_json: bool = _JSON) -> None:
    """List tasks."""
    tasks = _store(ctx).list()
    warnings = task_health_warnings(tasks)
    if as_json:
        emit_json({"tasks": [_task_dict(t) for t in tasks], "warnings": warnings})
        return
    if not tasks:
        log.info("No tasks.")
        _warn_tasks(tasks)
        return
    log.results_table([(t.id, t.status.value, t.title or t.objective[:48]) for t in tasks], title="Tasks")
    _warn_tasks(tasks)


@app.command("show")
def show_task(ctx: typer.Context, task_id: str = typer.Argument(...), as_json: bool = _JSON) -> None:
    """Show one task in full."""
    store = _store(ctx)
    try:
        task = store.get(task_id)
    except Exception:
        log.error(f"No task {task_id!r}.")
        raise typer.Exit(1)
    warnings = task_health_warnings(store.list())
    if as_json:
        emit_json({**_task_dict(task), **({"warnings": warnings} if warnings else {})})
        return
    data = _task_dict(task)
    comments = data.pop("comments", [])
    log.info(str(data))
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        author = str(comment.get("author") or "local")
        at = str(comment.get("at") or "")
        log.panel(str(comment.get("body") or ""), title=f"{author} {at}".strip())
    _warn_tasks(store.list())


@app.command("set")
def set_task(
    ctx: typer.Context,
    task_id: str = typer.Argument(..., help="Task id."),
    status: str | None = typer.Option(None, "--status", help="queued|running|blocked|done|failed|cancelled."),
    priority: str | None = typer.Option(None, "--priority", help="urgent|high|normal|low."),
    objective: str | None = typer.Option(None, "--objective"),
    title: str | None = typer.Option(None, "--title"),
    roadmap_ref: list[str] = typer.Option(None, "--roadmap-ref", help="Link to roadmap item id(s) (repeatable; replaces existing)."),
    inbox_ref: list[str] = typer.Option(None, "--inbox-ref", help="Link to inbox item id(s) (repeatable; replaces existing)."),
    author: str | None = typer.Option(None, "--author", help="Who is making the change (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Update fields of an existing task (safe YAML write).

    Open to agents and humans alike: the machine only ever writes queued/running,
    so a task reaches a terminal status (done/blocked/failed) only when an agent
    (or human) records it here. Declare ``done`` only when the work is *fully*
    complete."""
    store, rstore = _stores(ctx)
    try:
        task = store.get(task_id)
    except Exception:
        log.error(f"No task {task_id!r}.")
        raise typer.Exit(1)
    actor = author or agent_author() or task.metadata.get("author")
    changes: dict[str, object] = {"updated_at": utc_now()}
    new_status: TaskStatus | None = None
    if status is not None:
        new_status = TaskStatus(status.lower())
        changes["status"] = new_status
        if task.status != new_status:
            store.append_history(task_id, history_entry(actor, "status",
                                 before=task.status.value, after=new_status.value))
            _sync_roadmap_refs_from_task(rstore, task, new_status, actor)
    if priority is not None:
        changes["priority"] = priority
    if objective is not None:
        changes["objective"] = objective
    if title is not None:
        changes["title"] = title
    if roadmap_ref:
        changes["roadmap_refs"] = tuple(roadmap_ref)
    if inbox_ref:
        changes["inbox_refs"] = tuple(inbox_ref)
    edited = [f for f in changes if f not in ("status", "updated_at")]
    if edited:
        store.append_history(task_id, history_entry(actor, "edited", note=", ".join(edited) + " updated"))
    updated = store.put(dataclasses.replace(task, **changes))
    tasks = store.list()
    if as_json:
        emit_json(_with_task_warnings(_task_dict(updated), tasks))
        return
    log.success(f"Updated task {task_id}.")
    _warn_tasks(tasks)


def _sync_roadmap_refs_from_task(store, task: HorizonTask, status: TaskStatus, actor: str | None) -> None:
    target = roadmap_status_for_task_status(status)
    if target is None or not task.roadmap_refs:
        return
    roadmap = store.load()
    wanted = set(task.roadmap_refs)
    changed = False
    items = []
    for item in roadmap.items:
        if item.id not in wanted or item.status == target:
            items.append(item)
            continue
        # Propagate the status to the linked milestone and record a structured
        # history entry — but do NOT write a prose comment. The comment duplicated
        # progress narration that already lives in the task and its commits (the
        # "same comments in roadmap and task" problem); the history entry is enough.
        store.append_history(
            item.id,
            history_entry(
                actor,
                "status",
                before=item.status.value,
                after=target.value,
                note=f"synced from task {task.id}",
            ),
        )
        items.append(dataclasses.replace(
            item,
            status=target,
            metadata={**item.metadata, "updated_at": utc_now().isoformat()},
        ))
        changed = True
    if changed:
        store.save(Roadmap(items=tuple(items), updated_at=utc_now()))
        # The synced status may complete a parent's subtree (or contradict a done
        # parent) — say so, never silently fix: the editor decides.
        from archon_horizon.core.roadmap import hierarchy_status_warnings

        warnings = [
            *hierarchy_status_warnings(tuple(items)),
            *roadmap_health_warnings(tuple(items)),
        ]
        for warning in warnings:
            log.warn(warning)


@app.command("add")
def add_task(
    ctx: typer.Context,
    task_id: str = typer.Option(..., "--id", help="New task id."),
    project: str | None = typer.Option(None, "--project", help="Primary project; defaults to the session's project."),
    objective: str = typer.Option(..., "--objective", help="What the task should accomplish."),
    title: str = typer.Option("", "--title"),
    projects: list[str] = typer.Option(None, "--projects", help="All projects the task spans (repeatable)."),
    files: list[str] = typer.Option(None, "--file", help="Write-scope file glob (repeatable)."),
    priority: str = typer.Option("normal", "--priority"),
    status: str = typer.Option("queued", "--status"),
    author: str | None = typer.Option(None, "--author", help="Who is adding the task (ground|horizon|human)."),
    as_json: bool = _JSON,
) -> None:
    """Add a new task (safe YAML write). Open to agents and humans alike."""
    if not project:
        project = provenance_project()
        if not project:
            raise typer.BadParameter("pass --project (no session project to default from)")
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
    tasks = store.list()
    if as_json:
        emit_json(_with_task_warnings(_task_dict(task), tasks))
        return
    log.success(f"Added task {task_id}.")
    _warn_tasks(tasks)


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
    actor = author or agent_author()
    try:
        ensure_concise_agent_message(body, "task comment", author=actor)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    store.add_comment(task_id, body, actor, with_provenance())
    tasks = store.list()
    if as_json:
        emit_json(_with_task_warnings({"id": task_id, "commented": True}, tasks))
        return
    log.success(f"Commented on task {task_id}.")
    _warn_tasks(tasks)


@app.command("remove")
def remove_task(ctx: typer.Context, task_id: str = typer.Argument(...)) -> None:
    """Remove a task (deletes its YAML file). Open to agents and humans alike."""
    store = _store(ctx)
    store.delete(task_id)
    log.success(f"Removed task {task_id}.")
    _warn_tasks(store.list())
